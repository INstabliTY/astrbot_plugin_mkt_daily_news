"""
AstrBot 资讯助理插件 (Information Assistant)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
聚合天气、提醒、纯文本新闻、汇率与 API 余额监控。

Author : INstabliTY
Version: v1.3.2
"""

import asyncio
import datetime
import json
import os
import sqlite3
import traceback

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain


# ---------------------------------------------------------------------------
# 辅助常量
# ---------------------------------------------------------------------------
_DIVIDER = "\n\n---------------------------\n\n"


# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------

@register(
    "astrbot_plugin_Information_Assistant",
    "资讯助理",
    "聚合天气、提醒、纯文本新闻与汇率",
    "1.3.1",
)
class InformationAssistantPlugin(Star):
    """资讯助理插件主类。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self._parse_config(config or {})

        # 数据持久化路径：通过框架提供的 StarTools.get_data_dir() 获取规范目录，
        # 返回 pathlib.Path 对象，防止插件更新/重装时数据被覆盖。
        data_dir = StarTools.get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.reminders_file: str = str(data_dir / "reminders.json")
        self._ensure_reminders_file()
        # 文件操作锁：防止并发写入时后写覆盖先写，导致提醒数据丢失。
        self._file_lock: asyncio.Lock = asyncio.Lock()

        # 按框架指南推荐，直接在 __init__ 中用 asyncio.create_task() 注册后台任务。
        # AstrBot 的插件加载机制保证 __init__ 在事件循环运行后才被调用，
        # 因此 create_task 在此处始终安全。
        self._push_task: asyncio.Task | None = None
        if self.enable_push:
            self._push_task = asyncio.create_task(self._push_loop())
            logger.info(f"[资讯助理] 定时任务已启动，每天 {self.push_time} 推送。")
        else:
            logger.info("[资讯助理] 定时推送已关闭，以纯被动模式运行。")

    def _parse_config(self, cfg: dict) -> None:
        """
        从配置字典中解析并绑定所有插件参数。
        兼容新版 UI 的嵌套卡片结构（{section: {key: value}}）
        与旧版缓存的扁平结构（{key: value}）。
        """
        def _get(section: str, key: str, default):
            section_data = cfg.get(section)
            if isinstance(section_data, dict) and key in section_data:
                return section_data[key]
            if key in cfg:
                return cfg[key]
            return default

        # 全局推送设置
        self.enable_push: bool = _get("push_settings", "enable_push", True)
        self.push_time: str = _get("push_settings", "push_time", "08:00")
        raw_groups = _get("push_settings", "target_groups", [])
        # 兼容字符串配置（如误填 "12345"）和标准列表两种形式
        if isinstance(raw_groups, list):
            self.target_groups: list[str] = [
                str(g).strip() for g in raw_groups if g and str(g).strip()
            ]
        elif isinstance(raw_groups, str) and raw_groups.strip():
            # 单个字符串视为逗号分隔的列表
            self.target_groups = [g.strip() for g in raw_groups.split(",") if g.strip()]
        else:
            self.target_groups = []
        self.timezone_offset: float = self._parse_tz_offset(
            _get("push_settings", "timezone_offset", "10")
        )

        # 天气模块
        self.enable_weather: bool = _get("weather_settings", "enable_weather", True)
        self.city: str = _get("weather_settings", "city", "北京")

        # 提醒模块
        self.enable_reminders: bool = _get("reminder_settings", "enable_reminders", True)

        # 汇率模块
        self.enable_exchange: bool = _get("exchange_settings", "enable_exchange", True)
        self.exchange_api_key: str = _get("exchange_settings", "exchange_api_key", "")
        self.base_currency: str = _get(
            "exchange_settings", "base_currency", "CNY"
        ).upper()
        raw_currencies = _get(
            "exchange_settings", "target_currencies", "USD,JPY,EUR,GBP,HKD,AUD"
        )
        # 兼容配置中心返回 list（多选框）或 str（逗号分隔）两种形式
        if isinstance(raw_currencies, list):
            self.target_currencies: list[str] = [
                c.strip().upper() for c in raw_currencies if isinstance(c, str) and c.strip()
            ]
        else:
            self.target_currencies = [
                c.strip().upper() for c in str(raw_currencies).split(",") if c.strip()
            ]

        # 余额监控
        self.enable_balance: bool = _get("balance_settings", "enable_balance", True)
        self.deepseek_key: str = _get("balance_settings", "deepseek_key", "")
        self.moonshot_key: str = _get("balance_settings", "moonshot_key", "")

        # 新闻模块
        self.enable_news: bool = _get("news_settings", "enable_news", True)

        # 提醒模块扩展
        self.reminder_provider: str = _get("reminder_settings", "reminder_provider", "")
        self.reminder_lookback_days: int = max(
            1, min(30, int(_get("reminder_settings", "reminder_lookback_days", 7) or 7))
        )

        # 高级设置
        self.request_timeout: int = max(
            5, min(120, int(_get("advanced_settings", "request_timeout", 20) or 20))
        )
        raw_order = _get("advanced_settings", "module_order",
                         "weather,reminders,exchange,balance,news")
        self.module_order: list[str] = [
            m.strip() for m in str(raw_order).split(",") if m.strip()
        ] or ["weather", "reminders", "exchange", "balance", "news"]

    # -----------------------------------------------------------------------
    # 初始化工具方法
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_tz_offset(raw: str) -> float:
        """将时区偏移配置项解析为浮点数，容错范围 UTC-12 ~ UTC+14。"""
        try:
            offset = float(str(raw).strip())
            return max(-12.0, min(14.0, offset))
        except (ValueError, TypeError):
            logger.warning(f"[资讯助理] 时区配置 '{raw}' 无效，已回退至 UTC+10。")
            return 10.0

    def _ensure_reminders_file(self) -> None:
        """若提醒文件不存在则初始化为空列表。"""
        if not os.path.exists(self.reminders_file):
            with open(self.reminders_file, "w", encoding="utf-8") as f:
                json.dump([], f)

    # -----------------------------------------------------------------------
    # 1. 天气模块
    # -----------------------------------------------------------------------

    async def fetch_weather(self, session: aiohttp.ClientSession) -> str:
        """获取今日天气及穿衣建议。"""
        if not self.city:
            return ""
        geo_url = (
            f"https://geocoding-api.open-meteo.com/v1/search"
            f"?name={self.city}&count=1&language=zh"
        )
        try:
            async with session.get(geo_url) as resp:
                geo_data = await resp.json()
            results = geo_data.get("results")
            if not results:
                logger.warning(f"[资讯助理] 天气地理编码无结果，城市：{self.city}")
                return f"🌤️ 【{self.city}天气】获取失败，请检查城市名拼写。"
            lat = results[0]["latitude"]
            lon = results[0]["longitude"]

            weather_url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                f"&daily=temperature_2m_max,temperature_2m_min"
                f",precipitation_probability_max,apparent_temperature_max"
                f"&timezone=auto"
            )
            async with session.get(weather_url) as resp:
                data = await resp.json()

            daily = data["daily"]
            temp_max = daily["temperature_2m_max"][0]
            temp_min = daily["temperature_2m_min"][0]
            rain_prob = daily["precipitation_probability_max"][0]

            umbrella = "☔ 降水概率高，出门带伞！" if rain_prob > 40 else "🌂 降水概率低，无需带伞。"
            feels_max = daily.get("apparent_temperature_max", [None])[0]
            # 以体感最高温为穿衣参考（更符合实际感受）；不可用时退回实际最高温
            ref_temp = feels_max if feels_max is not None else temp_max
            if ref_temp >= 32:
                clothes = "🩳 高温酷热，建议清凉短袖/短裤，注意防晒补水。"
            elif ref_temp >= 26:
                clothes = "👕 温热，短袖即可，外出注意防晒。"
            elif ref_temp >= 20:
                clothes = "🧢 温暖舒适，轻薄上衣即可，早晚可备薄外套。"
            elif ref_temp >= 14:
                clothes = "🧥 微凉，建议长袖或薄外套。"
            elif ref_temp >= 6:
                clothes = "🧣 较冷，建议加厚外套或毛衣。"
            else:
                clothes = "🥶 严寒，建议厚羽绒服，注意保暖防风。"

            feels_line = f"  （体感最高 {feels_max:.0f}℃）" if feels_max is not None else ""
            return (
                f"🌤️ 【{self.city}今日天气】\n"
                f"🌡️ 温度：{temp_min}℃ ~ {temp_max}℃{feels_line}\n"
                f"🌧️ 降水概率：{rain_prob}%\n"
                f"{umbrella}\n{clothes}"
            )
        except aiohttp.ClientError as exc:
            logger.warning(f"[资讯助理] 天气请求网络错误：{exc}")
            return f"🌤️ 【{self.city}天气】网络请求失败。"
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(f"[资讯助理] 天气数据解析错误：{exc}")
            return f"🌤️ 【{self.city}天气】数据解析异常。"
        except asyncio.TimeoutError:
            return f"🌤️ 【{self.city}天气】请求超时。"

    # -----------------------------------------------------------------------
    # 2. 提醒模块
    # -----------------------------------------------------------------------

    async def _load_reminders(self) -> list[dict]:
        """
        从磁盘异步读取提醒列表，并对结构进行校验与清洗。
        确保返回值始终是 list[dict]，且每个元素包含合法的 date/content 字段，
        防止文件损坏或格式异常导致后续操作崩溃。
        """
        def _read():
            if not os.path.exists(self.reminders_file):
                return []
            with open(self.reminders_file, "r", encoding="utf-8") as f:
                return json.load(f)

        try:
            raw = await asyncio.to_thread(_read)
        except json.JSONDecodeError:
            logger.warning("[资讯助理] reminders.json JSON 解析失败，已返回空列表。")
            return []
        except OSError as exc:
            logger.warning(f"[资讯助理] reminders.json 读取 IO 错误：{exc}")
            return []

        # 结构校验：确保是列表，且每个元素是含 date/content 字符串字段的字典
        if not isinstance(raw, list):
            logger.warning(
                f"[资讯助理] reminders.json 根结构非列表（实为 {type(raw).__name__}），已重置。"
            )
            return []

        valid: list[dict] = []
        for item in raw:
            if (
                isinstance(item, dict)
                and isinstance(item.get("date"), str)
                and isinstance(item.get("content"), str)
            ):
                valid.append(item)
            else:
                logger.debug(f"[资讯助理] 跳过不合法的提醒条目：{item!r}")
        kept, dropped = self._purge_expired(valid)
        # 若有过期条目被清除，触发一次持锁原子回写，保持磁盘与内存同步。
        if dropped:
            asyncio.create_task(self._persist_purge(kept))
        return kept

    def _purge_expired(self, reminders: list[dict]) -> list[dict]:
        """
        过滤掉早于今天的过期提醒，防止文件无限膨胀。
        仅返回清理后的内存列表；将结果写回磁盘的职责由调用方（_load_reminders）
        在检测到有条目被删除时通过 _schedule_purge_write() 异步完成，
        保持本方法为纯同步、无 IO 副作用的函数。
        """
        tz = datetime.timezone(datetime.timedelta(hours=self.timezone_offset))
        today = datetime.datetime.now(tz).date()
        kept: list[dict] = []
        dropped = 0
        for r in reminders:
            try:
                item_date = datetime.date.fromisoformat(r["date"])
                if item_date >= today:
                    kept.append(r)
                else:
                    dropped += 1
            except (ValueError, KeyError):
                kept.append(r)  # 格式异常的条目保留，由上游校验处理
        if dropped:
            logger.debug(f"[资讯助理] 内存中清除 {dropped} 条过期提醒，将异步回写磁盘。")
        return kept, dropped  # 返回 dropped 数量供调用方决策是否回写

    async def _persist_purge(self, reminders: list[dict]) -> None:
        """将过期清理后的提醒列表回写磁盘（在 _file_lock 保护下），
        保证磁盘文件与内存状态一致，防止重启后过期数据复活。
        """
        async with self._file_lock:
            try:
                await self._atomic_write(reminders)
                logger.debug("[资讯助理] 过期提醒已回写磁盘。")
            except OSError as exc:
                logger.warning(f"[资讯助理] 过期提醒回写失败（非致命）：{exc}")

    async def _atomic_write(self, reminders: list[dict]) -> None:
        """
        底层原子写入：先写 .tmp 临时文件，再 os.replace() 替换目标文件。
        即使写入途中中断，也不会损坏已有的 reminders.json。
        调用方负责持有 _file_lock，此方法本身不加锁。
        """
        tmp_file = self.reminders_file + ".tmp"

        def _do_write():
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(reminders, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self.reminders_file)

        await asyncio.to_thread(_do_write)

    async def _add_reminder(self, date_str: str, content: str) -> str:
        """
        将一条用户自定义提醒写入本地 reminders.json。

        此方法服务于 /添加提醒 手动指令，作为 AstrBot 系统 cron_jobs 之外的
        补充通道：用户可以用自然语言指令直接录入不需要定时触发的备忘事项。

        整个「读 → 改 → 写」事务在同一把 _file_lock 下串行执行，防止并发竞态。
        """
        try:
            parsed = datetime.datetime.strptime(date_str.strip(), "%Y-%m-%d")
            standard_date = parsed.strftime("%Y-%m-%d")
        except ValueError:
            return f"❌ 日期格式错误：收到 '{date_str}'，请使用 YYYY-MM-DD 格式。"

        async with self._file_lock:
            # 在锁内完成读→改→写。直接调用不加锁的 _atomic_write，
            # 防止 asyncio.Lock（非可重入）在同一协程中二次 acquire 死锁。
            reminders = await self._load_reminders()
            reminders.append({"date": standard_date, "content": content.strip()})
            reminders.sort(key=lambda r: r["date"])
            try:
                await self._atomic_write(reminders)
            except OSError as exc:
                logger.error(f"[资讯助理] 保存提醒失败：{exc}")
                return "❌ 系统错误：提醒保存失败，请重试。"

        return f"✅ 资讯助理待办已记录：\n📅 {standard_date}\n📝 {content.strip()}"

    def _find_db_path(self) -> str | None:
        """
        定位 AstrBot 主数据库的绝对路径，兼容多种部署方式。

        已知路径规律（基于 AstrBot 源码及日志分析）：
          - 源码/uv/pip 部署：工作目录即 AstrBot 根目录，数据库在 data/data_v3.db 或 data/data.db
          - Docker 部署：通常挂载到 /AstrBot/data/ 或 /app/data/
          - 桌面客户端：~/.astrbot/data/
        """
        home = os.path.expanduser("~")
        candidates = [
            # 桌面客户端 / Windows（实际确认路径）
            os.path.join(home, ".astrbot", "data", "data_v4.db"),
            os.path.join(home, ".astrbot", "data", "data_v3.db"),
            os.path.join(home, ".astrbot", "data", "data.db"),
            # 源码/uv/pip 部署（工作目录为 AstrBot 根）
            os.path.join("data", "data_v4.db"),
            os.path.join("data", "data_v3.db"),
            os.path.join("data", "data.db"),
            os.path.join("data", "astrbot.db"),
            # Docker 常见挂载路径
            os.path.join("/AstrBot", "data", "data_v4.db"),
            os.path.join("/AstrBot", "data", "data_v3.db"),
            os.path.join("/AstrBot", "data", "data.db"),
            os.path.join("/app", "data", "data_v4.db"),
            os.path.join("/app", "data", "data_v3.db"),
            os.path.join("/app", "data", "data.db"),
        ]
        for path in candidates:
            if os.path.exists(path):
                logger.debug(f"[资讯助理] 找到 AstrBot 数据库：{path}")
                return os.path.abspath(path)
        logger.debug(f"[资讯助理] 未找到 AstrBot 数据库，已搜索路径：{candidates}")
        return None


    async def _load_system_tasks(self) -> list[dict]:
        """
        从 AstrBot 主数据库（data_v4.db）的 cron_jobs 表读取系统级提醒，
        归一化为 {date, content} 格式后返回。

        已通过数据库文件确认的真实 Schema：
          表名      : cron_jobs
          content  : description 字段（与 payload.note 相同，始终有值）
          时间     : payload JSON 中的 run_at（ISO 8601 含时区）
                     若 payload 无 run_at，回退到 next_run_time 字段
          过滤条件 : status = 'scheduled' AND enabled = 1

        只读操作，不修改数据库，不干扰框架 cron 执行逻辑。
        """
        db_path = self._find_db_path()
        if not db_path:
            return []

        tz_offset = self.timezone_offset  # 闭包捕获，避免线程中访问 self

        def _query() -> list[dict]:
            results: list[dict] = []
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()

                # 只取待执行、已启用的任务
                cur.execute(
                    "SELECT name, description, next_run_time, payload "
                    "FROM cron_jobs "
                    "WHERE status = 'scheduled' AND enabled = 1"
                )
                rows = cur.fetchall()
                conn.close()

                tz = datetime.timezone(datetime.timedelta(hours=tz_offset))
                today = datetime.datetime.now(tz).date()

                for row in rows:
                    # 优先从 payload.run_at 获取含时区的时间，确保时区转换准确
                    run_at_str: str | None = None
                    try:
                        payload = json.loads(row["payload"] or "{}")
                        run_at_str = payload.get("run_at")
                    except (json.JSONDecodeError, TypeError):
                        payload = {}

                    # 回退到 next_run_time（无时区，视为插件配置时区）
                    if not run_at_str:
                        run_at_str = row["next_run_time"] or ""

                    if not run_at_str:
                        continue

                    try:
                        dt = datetime.datetime.fromisoformat(run_at_str)
                        # next_run_time 无时区信息时，附加配置时区
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=tz)
                        dt_local = dt.astimezone(tz)
                        item_date = dt_local.date()
                        if item_date < today:
                            continue  # 跳过已过期任务
                        date_str = item_date.strftime("%Y-%m-%d")
                    except (ValueError, TypeError):
                        continue

                    # description 即提醒全文，始终有值
                    content = (row["description"] or row["name"] or "").strip()
                    if content:
                        run_time = dt_local.strftime("%H:%M")
                        results.append({
                            "date": date_str,
                            "content": content,
                            "run_time": run_time,
                        })

            except sqlite3.OperationalError as exc:
                logger.debug(f"[资讯助理] 系统任务数据库只读访问：{exc}")
            except sqlite3.Error as exc:
                logger.warning(f"[资讯助理] 读取 cron_jobs 失败：{exc}")
            return results

        try:
            return await asyncio.to_thread(_query)
        except Exception as exc:
            logger.warning(f"[资讯助理] 系统任务查询异常：{exc}")
            return []

    async def _parse_reminder_text(self, raw: str, date_str: str, run_time: str = "") -> str:
        """
        用 AstrBot 内置 LLM 将原始提醒文本提炼为一行简洁摘要。
        格式：{标签}{核心事项}  {时间描述}
        例：【ECON7000】CLEAR-JE 1 DDL  明天下午4点（3月20日周五）
        LLM 不可用时自动降级为截取原文前60字。
        """
        provider = None
        try:
            # 优先使用用户在配置中指定的模型；留空则使用当前默认提供商
            if self.reminder_provider:
                provider = self.context.get_provider_by_id(self.reminder_provider)
            if provider is None:
                provider = self.context.get_using_provider()
        except Exception:
            pass

        if provider is None:
            return raw[:60] + ("…" if len(raw) > 60 else "")

        time_info = ("，该提醒的实际执行时间为 " + run_time) if run_time else ""
        time_anchor = run_time if run_time else date_str
        prompt_lines = [
            "今天是 " + date_str + time_info + "。",
            "请将以下提醒文本提炼为一行简洁摘要，严格按格式输出：",
            "  「标签」核心事项  时间描述",
            "规则（必须遵守）：",
            "1. 标签：原文【】内的课程/项目名，用「」包裹；若无方括号则省略标签",
            "2. 核心事项：最简动宾短语，删去解释、提问、感叹句",
            "3. 时间描述：基于实际执行时间 " + time_anchor + " 生成，",
            "   ★ 严禁直接复制原文中出现的「明天」「后天」「本周」等相对时间词",
            "   ★ 若有具体时刻（如 09:00 或 08:00-11:00），必须完整输出",
            "   ★ 若无具体时刻，时间描述部分留空，不要输出任何内容",
            "4. 只输出结果这一行，不要加任何解释",
            "原始提醒：" + raw,
        ]
        prompt = "\n".join(prompt_lines)

        # 最多重试 2 次，遇到 429/过载错误时指数退避等待
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await provider.text_chat(
                    prompt=prompt,
                    session_id=None,
                    image_urls=[],
                    func_tool=None,
                )
                first_line = resp.completion_text.strip().split("\n")[0].strip()
                return first_line if first_line else raw[:60]
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc)
                # 429 / 过载 / 限流 → 等待后重试；其他错误直接降级
                if any(k in exc_str for k in ("429", "overload", "rate_limit", "too many")):
                    wait = 3 * (attempt + 1)  # 3s, 6s
                    logger.debug(f"[资讯助理] 提醒格式化遇到限流（{exc_str[:60]}），{wait}s 后重试（第{attempt+1}次）")
                    await asyncio.sleep(wait)
                else:
                    break  # 非限流错误，不重试
        logger.debug(f"[资讯助理] 提醒格式化 LLM 调用失败：{last_exc}")
        return raw[:60] + ("…" if len(raw) > 60 else "")

    async def format_reminders(self) -> str:
        """
        格式化输出今日待办和本周预警。

        数据来源合并两路：
        1. 插件自有 reminders.json（/添加提醒 手动指令写入的用户自定义备忘）
        2. AstrBot 系统 cron_jobs 数据库（LLM 通过 create_future_task 添加的提醒）

        每条提醒原文经 _parse_reminder_text() 用 LLM 提炼为简洁一行摘要，
        若 LLM 不可用则自动降级为截断原文。
        两路数据去重后统一展示。
        """
        json_reminders, sys_tasks = await asyncio.gather(
            self._load_reminders(),
            self._load_system_tasks(),
        )

        # 合并并去重（以 date+content 原文为 key，防止同一条目从两路各出现一次）
        seen: set[tuple[str, str]] = set()
        reminders: list[dict] = []
        for r in json_reminders + sys_tasks:
            key = (r.get("date", ""), r.get("content", ""))
            if key not in seen:
                seen.add(key)
                reminders.append(r)

        tz = datetime.timezone(datetime.timedelta(hours=self.timezone_offset))
        now = datetime.datetime.now(tz)
        today_str = now.strftime("%Y-%m-%d")
        this_week = {
            (now + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(1, self.reminder_lookback_days + 1)
        }

        today_items: list[dict] = []
        week_items: list[dict] = []

        for r in reminders:
            try:
                d = datetime.datetime.strptime(r["date"], "%Y-%m-%d").strftime("%Y-%m-%d")
            except (ValueError, KeyError, TypeError):
                continue
            if d == today_str:
                today_items.append(r)
            elif d in this_week:
                week_items.append(r)

        if not today_items and not week_items:
            return "📝 【提醒事项】近期无安排，享受生活吧！"

        # ── 相对日期标签生成（未来提醒用）─────────────────────────────────
        def _relative_label(item_date_str: str) -> str:
            """将 YYYY-MM-DD 转为 MM-DD（明天/后天/周几/下周几）格式。"""
            try:
                item_date = datetime.date.fromisoformat(item_date_str)
            except ValueError:
                return item_date_str[5:]
            today_date = now.date()
            delta = (item_date - today_date).days
            mm_dd = item_date.strftime("%m-%d")
            weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            if delta == 1:
                rel = "明天"
            elif delta == 2:
                rel = "后天"
            elif delta <= 6:
                rel = weekday_cn[item_date.weekday()]
            else:
                rel = "下" + weekday_cn[item_date.weekday()]
            return mm_dd + "（" + rel + "）"

        # ── 限速 LLM 格式化（最多 2 路并发，避免触发限流）────────────────────
        all_items = (
            [(r, today_str, True) for r in today_items]
            + [(r, r.get("date", today_str), False) for r in week_items]
        )

        # Semaphore 限制同时进行的 LLM 调用数量，防止并发过高触发 429
        _llm_sem = asyncio.Semaphore(2)

        async def _fmt(r: dict, date_for_ctx: str, idx: int) -> str:
            async with _llm_sem:
                # 每次调用前稍作错峰，降低短时并发压力
                if idx > 0:
                    await asyncio.sleep(0.5 * idx)
                return await self._parse_reminder_text(
                    r.get("content", ""),
                    date_for_ctx,
                    r.get("run_time", ""),
                )

        fmt_results = await asyncio.gather(
            *[_fmt(r, date_ctx, i) for i, (r, date_ctx, _) in enumerate(all_items)],
            return_exceptions=True,
        )

        # ── 组装输出 ─────────────────────────────────────────────────────────
        fmt_today: list[tuple[str, str]] = []   # (run_time, text)
        fmt_future: list[tuple[str, str]] = []  # (label, text)

        for i, (r, date_ctx, is_today) in enumerate(all_items):
            result = fmt_results[i]
            text = result if isinstance(result, str) else r.get("content", "")[:60]
            run_time = r.get("run_time", "")
            if is_today:
                fmt_today.append((run_time, text))
            else:
                label = _relative_label(r.get("date", ""))
                fmt_future.append((label, text))

        parts: list[str] = []
        if fmt_today:
            # 有时间的按 HH:MM 排序，无时间的排在末尾
            fmt_today.sort(key=lambda x: x[0] if x[0] else "99:99")
            lines_today = []
            for run_time, text in fmt_today:
                if run_time:
                    lines_today.append(run_time + " " + text)
                else:
                    lines_today.append(text)
            parts.append("📝 【今日待办】\n" + "\n".join("🔔 " + t for t in lines_today))
        if fmt_future:
            lines_future = [label + ": " + text for label, text in fmt_future]
            parts.append("📅 【未来提醒】\n" + "\n".join("📌 " + t for t in lines_future))

        return "\n\n".join(parts)

    # -----------------------------------------------------------------------
    # 3. 新闻模块
    # -----------------------------------------------------------------------

    async def fetch_60s_news_text(self, session: aiohttp.ClientSession) -> str:
        """获取 60s 纯文本新闻（多 URL 降级容错）。"""
        urls = [
            "https://60s.viki.moe/v2/60s",
            "https://60s-api.114128.xyz/v2/60s",
        ]
        for url in urls:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"[资讯助理] 新闻接口 {url} 返回 HTTP {resp.status}")
                        continue
                    data = await resp.json()
                    news_items: list[str] = data.get("data", {}).get("news", [])
                    if not news_items:
                        continue
                    body = "\n \n".join(
                        f"{i}. {item}" for i, item in enumerate(news_items, 1)
                    )
                    return f"📰 【每日60s纯文本速报】\n\n{body}"
            except aiohttp.ClientError as exc:
                logger.warning(f"[资讯助理] 新闻接口 {url} 网络错误：{exc}")
                continue
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning(f"[资讯助理] 新闻接口 {url} 数据解析错误：{exc}")
                continue
            except asyncio.TimeoutError:
                logger.warning(f"[资讯助理] 新闻接口 {url} 请求超时，尝试备用接口。")
                continue
        return "📰 【新闻速报】获取失败，接口波动。"

    # -----------------------------------------------------------------------
    # 4. 汇率模块
    # -----------------------------------------------------------------------

    async def fetch_exchange_rates(self, session: aiohttp.ClientSession) -> str:
        """获取实时汇率（以 base_currency 为基准）。"""
        if not self.exchange_api_key:
            return "📊 【汇率】⚠️ 未配置 API Key。"
        url = (
            f"https://v6.exchangerate-api.com/v6/{self.exchange_api_key}"
            f"/latest/{self.base_currency}"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[资讯助理] 汇率 API 返回 HTTP {resp.status}，"
                        f"接口域名：exchangerate-api.com，货币：{self.base_currency}"
                    )
                    return "📊 【汇率】API 请求失败。"
                data = await resp.json()
            rates = data.get("conversion_rates", {})
            lines: list[str] = []
            for cur in self.target_currencies:
                rate = rates.get(cur)
                if rate and rate != 0:
                    # rate = 1 {base} 能换多少 {cur}
                    # 用户更关心的是"花多少本币换100外币"，即反向汇率
                    cost = 100 / rate  # 100 {cur} 需要多少 {base}
                    lines.append(
                        f"- {cur}：1 {self.base_currency} = {rate:.4f} {cur}"
                        f"  |  100 {cur} ≈ {cost:.2f} {self.base_currency}"
                    )
            if not lines:
                return "📊 【汇率】暂无有效汇率数据。"
            return f"📊 【实时汇率】\n" + "\n".join(lines)
        except aiohttp.ClientError as exc:
            logger.warning(f"[资讯助理] 汇率请求网络错误：{exc}")
            return "📊 【汇率】网络请求失败。"
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning(f"[资讯助理] 汇率数据解析错误：{exc}")
            return "📊 【汇率】数据解析异常。"
        except asyncio.TimeoutError:
            return "📊 【汇率】请求超时。"

    # -----------------------------------------------------------------------
    # 5. API 余额监控
    # -----------------------------------------------------------------------

    async def fetch_deepseek_balance(self, session: aiohttp.ClientSession) -> str:
        """查询 DeepSeek API 余额。"""
        if not self.deepseek_key:
            return "- DeepSeek: 未配置"
        try:
            headers = {"Authorization": f"Bearer {self.deepseek_key}"}
            async with session.get(
                "https://api.deepseek.com/user/balance", headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[资讯助理] DeepSeek 余额 API 返回 HTTP {resp.status}")
                    return "- DeepSeek: 查询失败"
                data = await resp.json()
            infos: list[dict] = data.get("balance_infos", [])
            if infos:
                balances = " / ".join(
                    f"{i.get('total_balance')} {i.get('currency')}" for i in infos
                )
                return f"- DeepSeek: {balances}"
        except aiohttp.ClientError as exc:
            logger.warning(f"[资讯助理] DeepSeek 请求网络错误：{exc}")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning(f"[资讯助理] DeepSeek 数据解析错误：{exc}")
        except asyncio.TimeoutError:
            logger.warning("[资讯助理] DeepSeek 余额查询超时")
        return "- DeepSeek: 查询异常"

    async def fetch_moonshot_balance(self, session: aiohttp.ClientSession) -> str:
        """查询 Moonshot (Kimi) API 余额。"""
        if not self.moonshot_key:
            return "- Kimi: 未配置"
        try:
            headers = {"Authorization": f"Bearer {self.moonshot_key}"}
            async with session.get(
                "https://api.moonshot.cn/v1/users/me/balance", headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[资讯助理] Kimi 余额 API 返回 HTTP {resp.status}")
                    return "- Kimi: 查询失败"
                data = await resp.json()
            available = data.get("data", {}).get("available_balance", 0)
            return f"- Kimi: ￥{available:.2f}"
        except aiohttp.ClientError as exc:
            logger.warning(f"[资讯助理] Kimi 请求网络错误：{exc}")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning(f"[资讯助理] Kimi 数据解析错误：{exc}")
        except asyncio.TimeoutError:
            logger.warning("[资讯助理] Kimi 余额查询超时")
        return "- Kimi: 查询异常"

    # -----------------------------------------------------------------------
    # 核心情报组装
    # -----------------------------------------------------------------------

    async def build_news_text(self) -> str:
        """并发拉取所有数据模块，组装成最终情报文本。"""
        logger.info("[资讯助理] 开始并发拉取情报...")

        async def _skip() -> str:
            return ""

        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            results = await asyncio.gather(
                self.fetch_weather(session) if self.enable_weather else _skip(),
                self.fetch_exchange_rates(session) if self.enable_exchange else _skip(),
                self.fetch_deepseek_balance(session) if self.enable_balance else _skip(),
                self.fetch_moonshot_balance(session) if self.enable_balance else _skip(),
                self.fetch_60s_news_text(session) if self.enable_news else _skip(),
                return_exceptions=True,
            )

        def _safe(result, fallback: str) -> str:
            return result if not isinstance(result, Exception) else fallback

        weather_text = _safe(results[0], "🌤️ 【天气】获取超时")
        exchange_text = _safe(results[1], "📊 【汇率】获取超时")
        ds_balance = _safe(results[2], "- DeepSeek: 超时")
        ms_balance = _safe(results[3], "- Kimi: 超时")
        news_text = _safe(results[4], "📰 【新闻】获取超时")

        reminders_text = await self.format_reminders() if self.enable_reminders else ""

        # 按用户配置的 module_order 动态排列各板块
        balance_block = f"💰 【API 资产监控】\n{ds_balance}\n{ms_balance}"
        module_map: dict[str, str] = {
            "weather":   weather_text if self.enable_weather else "",
            "reminders": reminders_text if self.enable_reminders else "",
            "exchange":  exchange_text if self.enable_exchange else "",
            "balance":   balance_block if self.enable_balance else "",
            "news":      news_text if self.enable_news else "",
        }
        blocks: list[str] = [
            module_map[m] for m in self.module_order
            if m in module_map and module_map[m]
        ]

        if not blocks:
            return "📭 资讯助理：所有情报模块已在后台关闭。"
        return _DIVIDER.join(blocks)

    # -----------------------------------------------------------------------
    # 定时推送循环（asyncio 原生实现，无需第三方调度器）
    # -----------------------------------------------------------------------

    async def _push_loop(self) -> None:
        """
        持续运行的定时推送循环。
        每次计算距下次推送时间的剩余秒数并 sleep，避免漂移。
        """
        hour, minute = self._parse_push_time(self.push_time)
        logger.info(f"[资讯助理] 推送循环启动，目标时间 {hour:02d}:{minute:02d}。")

        while True:
            seconds = self._seconds_until(hour, minute)
            logger.debug(f"[资讯助理] 距下次推送还有 {seconds:.0f} 秒。")
            await asyncio.sleep(seconds)
            try:
                await self._broadcast()
            except Exception:
                logger.error(f"[资讯助理] 广播异常：{traceback.format_exc()}")

    @staticmethod
    def _parse_push_time(push_time: str) -> tuple[int, int]:
        """将 'HH:MM' 字符串解析为 (hour, minute)，格式错误时返回默认值 (8, 0)。"""
        try:
            parts = push_time.strip().split(":")
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h, m
        except (ValueError, IndexError, AttributeError):
            pass
        logger.warning(f"[资讯助理] 推送时间 '{push_time}' 格式无效，已回退至 08:00。")
        return 8, 0

    def _seconds_until(self, hour: int, minute: int) -> float:
        """计算从当前时刻到指定时刻的剩余秒数（始终为正，跨越午夜时 +24h）。"""
        tz = datetime.timezone(datetime.timedelta(hours=self.timezone_offset))
        now = datetime.datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return (target - now).total_seconds()

    async def _broadcast(self) -> None:
        """向所有目标群组推送今日情报。"""
        if not self.target_groups:
            logger.warning("[资讯助理] 定时推送已触发，但未配置 target_groups，跳过。")
            return
        final_text = await self.build_news_text()
        chain = MessageChain([Plain(final_text)])
        for target in self.target_groups:
            try:
                await self.context.send_message(target, chain)
                logger.info(f"[资讯助理] 早报已送达：{target}")
            except Exception as exc:
                logger.error(f"[资讯助理] 送达 '{target}' 失败：{exc}")
            await asyncio.sleep(1)

    # -----------------------------------------------------------------------
    # 指令处理器
    # -----------------------------------------------------------------------

    @filter.command("今日情报")
    async def manual_trigger(self, event: AstrMessageEvent):
        """手动触发一次完整情报推送。"""
        yield event.plain_result("🚀 资讯助理正在拉取情报（约 3-5 秒），请稍候...")
        text = await self.build_news_text()
        yield event.plain_result(text)

    @filter.command("提醒诊断")
    async def diagnose_reminders(self, event: AstrMessageEvent):
        """
        诊断指令：列出数据库路径、全部表名和列信息、原始任务数据。
        用法：/提醒诊断
        """
        lines: list[str] = ["🔍 【资讯助理提醒诊断 v2】"]

        # 1. 数据库路径
        db_path = self._find_db_path()
        if db_path:
            lines.append(f"\n✅ 数据库：{db_path}")
        else:
            lines.append("\n❌ 未找到数据库，请告知你的部署方式（源码/Docker/桌面版）")

        # 2. 列出数据库所有表名 + future_task 相关表的列信息
        if db_path:
            def _inspect():
                result = []
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                    cur = conn.cursor()
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                    all_tables = [row[0] for row in cur.fetchall()]
                    result.append(f"\n📂 全部表名（{len(all_tables)} 个）：")
                    result.append("  " + ", ".join(all_tables))
                    # 对所有表查列名，找 run_at / note 相关的
                    for tbl in all_tables:
                        cur.execute(f"PRAGMA table_info({tbl})")
                        cols = [row[1] for row in cur.fetchall()]
                        col_str = ", ".join(cols)
                        # 标注可能是 future_task 的表
                        markers = []
                        if any(c in cols for c in ("run_at", "note", "task_note")):
                            markers.append("⭐可能是任务表")
                        if markers:
                            result.append(f"\n  [{tbl}] {col_str}  <- {', '.join(markers)}")
                            # 顺带抓前5行原始数据
                            try:
                                cur.execute(f"SELECT * FROM {tbl} LIMIT 5")
                                rows = cur.fetchall()
                                for r in rows:
                                    result.append(f"    行：{dict(zip(cols, r))}")
                            except Exception:
                                pass
                    conn.close()
                except Exception as exc:
                    result.append(f"\n❌ 数据库读取失败：{exc}")
                return result
            inspection = await asyncio.to_thread(_inspect)
            lines.extend(inspection)

        # 3. 本地 JSON 提醒
        json_reminders = await self._load_reminders()
        lines.append(f"\n📄 reminders.json：{len(json_reminders)} 条")
        for r in json_reminders[:5]:
            lines.append(f"  [{r.get('date')}] {r.get('content', '')[:50]}")

        # 4. 时区
        tz = datetime.timezone(datetime.timedelta(hours=self.timezone_offset))
        now = datetime.datetime.now(tz)
        lines.append(f"\n🕐 时区 UTC+{self.timezone_offset}，当前：{now.strftime('%Y-%m-%d %H:%M')}")

        yield event.plain_result("\n".join(lines))

    @filter.command("添加提醒")
    async def add_reminder_cmd(self, event: AstrMessageEvent):
        """
        手动添加提醒指令。
        用法：/添加提醒 YYYY-MM-DD 事项内容
        示例：/添加提醒 2026-04-01 记得交作业
        """
        raw: str = event.message_str.strip()
        # 去掉命令前缀后的剩余部分
        body = raw.removeprefix("/添加提醒").removeprefix("添加提醒").strip()
        parts = body.split(None, 1)  # 按空白字符分割，最多分成 2 份
        if len(parts) < 2:
            yield event.plain_result(
                "❌ 格式错误。\n用法：/添加提醒 YYYY-MM-DD 事项内容\n"
                "示例：/添加提醒 2026-04-01 记得交作业"
            )
            return
        date_str, content = parts[0], parts[1]
        result = await self._add_reminder(date_str, content)
        yield event.plain_result(result)

    # -----------------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------------

    async def terminate(self) -> None:
        """插件卸载/重载时，取消后台推送任务。"""
        if self._push_task and not self._push_task.done():
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
        logger.info("[资讯助理] 插件已安全停止。")
