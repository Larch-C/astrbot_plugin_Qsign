import asyncio
import base64
import os
from datetime import datetime

import aiofiles
import aiohttp
import pytz
import yaml

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join("data", "astrbot_plugin_Qsign")
DATA_FILE = os.path.join(DATA_DIR, "sign_data.yml")
PURCHASE_DATA_FILE = os.path.join(DATA_DIR, "purchase_counts.yml")

# API配置
AVATAR_API = "http://q.qlogo.cn/headimg_dl?dst_uin={}&spec=640&img_type=jpg"

WEALTH_LEVELS = [
    (0, "平民", 0.25),
    (500, "小资", 0.5),
    (2000, "富豪", 0.75),
    (5000, "巨擘", 1.0),
]
WEALTH_BASE_VALUES = {"平民": 100.0, "小资": 500.0, "富豪": 2000.0, "巨擘": 5000.0}
BASE_INCOME = 100.0
SHANGHAI_TZ = pytz.timezone("Asia/Shanghai")


@register(
    "astrbot_plugin_sign",
    "长安某",
    "二次元签到插件",
    "2.0.0",
    "https://github.com/zgojin/astrbot_plugin_sign",
)
class ContractSystem(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.font_path = os.path.join(PLUGIN_DIR, "请以你的名字呼唤我.ttf")
        self.template_path = os.path.join(PLUGIN_DIR, "card_template.html")
        self.default_bg_path = os.path.join(PLUGIN_DIR, "default_bg.jpg")

        timeout = aiohttp.ClientTimeout(total=10)
        self.session = aiohttp.ClientSession(timeout=timeout)
        self._init_env()
        self.html_template = self._load_template()

        self.sign_data = {}
        self.purchase_data = {}
        asyncio.create_task(self._load_all_data_to_cache())

    @filter.regex(r"^购买")
    async def purchase(self, event: AstrMessageEvent):
        target_id = None
        for component in event.message_obj.message:
            if isinstance(component, At):
                target_id = str(component.qq)
                break

        if not target_id:
            yield event.plain_result("请使用@指定要购买的对象。")
            return

        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        if user_id == target_id:
            yield event.plain_result("您不能购买自己。")
            return

        employer_data = self._get_user_data(self.sign_data, group_id, user_id)
        target_data = self._get_user_data(self.sign_data, group_id, target_id)

        if len(employer_data["contractors"]) >= 3:
            yield event.plain_result("已达到最大雇佣数量（3人）。")
            return

        base_cost = self._calculate_dynamic_wealth_value(
            target_data, self.purchase_data, target_id
        )
        total_cost = base_cost
        original_owner_id = target_data.get("contracted_by")

        if original_owner_id:
            if original_owner_id == user_id:
                yield event.plain_result("该用户已经是您的雇员了。")
                return

            takeover_rate = self.config.get("takeover_fee_rate", 0.1)
            extra_cost = base_cost * takeover_rate
            total_cost += extra_cost
            compensation = total_cost

            if employer_data["coins"] < total_cost:
                yield event.plain_result(
                    f"现金不足，恶意收购需要支付 {total_cost:.1f} 金币（含{takeover_rate * 100}%额外费用）。"
                )
                return

            original_owner_data = self._get_user_data(
                self.sign_data, group_id, original_owner_id
            )
            if target_id in original_owner_data["contractors"]:
                original_owner_data["contractors"].remove(target_id)

            original_owner_data["coins"] += compensation
            employer_data["coins"] -= total_cost

            employer_data["contractors"].append(target_id)
            target_data["contracted_by"] = user_id

            self.purchase_data[target_id] = self.purchase_data.get(target_id, 0) + 1

            await self._save_yaml_async(self.sign_data, DATA_FILE)
            await self._save_yaml_async(self.purchase_data, PURCHASE_DATA_FILE)

            target_name = await self._get_user_name_from_platform(event, target_id)
            original_owner_name = await self._get_user_name_from_platform(
                event, original_owner_id
            )
            yield event.plain_result(
                f"恶意收购成功！您花费 {total_cost:.1f} 金币从 {original_owner_name} 手中抢走了 {target_name}。"
                f"原雇主获得了全部转让费 {compensation:.1f} 金币。"
            )
            return

        if employer_data["coins"] < total_cost:
            yield event.plain_result(
                f"现金不足，雇佣需要支付目标身价：{total_cost:.1f}金币。"
            )
            return

        employer_data["coins"] -= total_cost
        employer_data["contractors"].append(target_id)
        target_data["contracted_by"] = user_id

        self.purchase_data[target_id] = self.purchase_data.get(target_id, 0) + 1
        await self._save_yaml_async(self.sign_data, DATA_FILE)
        await self._save_yaml_async(self.purchase_data, PURCHASE_DATA_FILE)

        target_name = await self._get_user_name_from_platform(event, target_id)
        yield event.plain_result(f"成功雇佣 {target_name}，消耗{total_cost:.1f}金币。")

    @filter.regex(r"^出售")
    async def sell(self, event: AstrMessageEvent):
        target_id = None
        for component in event.message_obj.message:
            if isinstance(component, At):
                target_id = str(component.qq)
                break

        if not target_id:
            yield event.plain_result("请使用@指定要出售的对象。")
            return

        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        employer_data = self._get_user_data(self.sign_data, group_id, user_id)
        target_data = self._get_user_data(self.sign_data, group_id, target_id)
        if target_id not in employer_data["contractors"]:
            yield event.plain_result("该用户不在你的雇员列表中。")
            return

        sell_rate = self.config.get("sell_return_rate", 0.8)
        sell_price = (
            self._calculate_dynamic_wealth_value(
                target_data, self.purchase_data, target_id
            )
            * sell_rate
        )
        employer_data["coins"] += sell_price
        employer_data["contractors"].remove(target_id)
        target_data["contracted_by"] = None
        await self._save_yaml_async(self.sign_data, DATA_FILE)
        target_name = await self._get_user_name_from_platform(event, target_id)
        yield event.plain_result(
            f"成功解雇 {target_name}，获得补偿金{sell_price:.1f}金币。"
        )

    @filter.regex(r"^签到$")
    async def sign_in(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user_data = self._get_user_data(self.sign_data, group_id, user_id)
        now = datetime.now(SHANGHAI_TZ)
        today = now.date()
        if user_data["last_sign"]:
            last_sign_dt = datetime.fromisoformat(user_data["last_sign"])
            last_sign_aware = SHANGHAI_TZ.localize(last_sign_dt)
            if last_sign_aware.date() == today:
                yield event.plain_result("你今天已经签到过了，明天再来吧。")
                return
            if (today - last_sign_aware.date()).days == 1:
                user_data["consecutive"] += 1
            else:
                user_data["consecutive"] = 1
        else:
            user_data["consecutive"] = 1
        interest = user_data["bank"] * 0.01
        user_data["bank"] += interest
        _, user_base_rate = self._get_wealth_info(user_data)

        contractor_dynamic_rates = self._get_total_contractor_rate(
            group_id, user_data["contractors"]
        )

        consecutive_bonus = 10 * (user_data["consecutive"] - 1)
        earned = (
            BASE_INCOME * (1 + user_base_rate) * (1 + contractor_dynamic_rates)
            + consecutive_bonus
        )
        original_earned = earned
        is_penalized = False
        if user_data["contracted_by"]:
            income_rate = self.config.get("employed_income_rate", 0.7)
            earned *= income_rate
            is_penalized = True
        user_data["coins"] += earned
        user_data["last_sign"] = now.replace(tzinfo=None).isoformat()
        await self._save_yaml_async(self.sign_data, DATA_FILE)
        html_url = await self._generate_card_html(
            event,
            is_query=False,
            is_penalized=is_penalized,
            original_earned=original_earned,
        )
        if html_url:
            yield event.image_result(html_url)
        else:
            yield event.plain_result("签到成功！但图片生成失败。")

    @filter.regex(r"^(排行榜|财富榜)$")
    async def leaderboard(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        group_data = self.sign_data.get(group_id)
        if not group_data:
            yield event.plain_result("本群暂无签到数据，无法生成排行榜。")
            return
        all_users_wealth = []
        for user_id, user_data in group_data.items():
            total_wealth = user_data.get("coins", 0.0) + user_data.get("bank", 0.0)
            all_users_wealth.append((user_id, total_wealth))
        sorted_users = sorted(all_users_wealth, key=lambda item: item[1], reverse=True)
        top_10_users = sorted_users[:10]
        if not top_10_users:
            yield event.plain_result("本群暂无签到数据，无法生成排行榜。")
            return
        user_ids_to_fetch = [user[0] for user in top_10_users]
        name_coroutines = [
            self._get_user_name_from_platform(event, uid) for uid in user_ids_to_fetch
        ]
        names = await asyncio.gather(*name_coroutines)

        leaderboard_str = "本群财富排行榜\n" + "-" * 20 + "\n"
        for rank, ((user_id, total_wealth), user_name) in enumerate(
            zip(top_10_users, names), start=1
        ):
            leaderboard_str += f"第{rank}名: {user_name} - {total_wealth:.1f} 金币\n"

        yield event.plain_result(leaderboard_str.strip())

    @filter.regex(r"^赎身$")
    async def terminate_contract(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user_data = self._get_user_data(self.sign_data, group_id, user_id)
        if not user_data["contracted_by"]:
            yield event.plain_result("您是自由身，无需赎身。")
            return

        cost = self._calculate_dynamic_wealth_value(
            user_data, self.purchase_data, user_id
        )
        if user_data["coins"] < cost:
            yield event.plain_result(f"金币不足，需要支付赎身费用：{cost:.1f}金币。")
            return

        employer_id = user_data["contracted_by"]
        employer_data = self._get_user_data(self.sign_data, group_id, employer_id)

        user_data["coins"] -= cost
        if user_id in employer_data["contractors"]:
            employer_data["contractors"].remove(user_id)
        user_data["contracted_by"] = None

        redeem_rate = self.config.get("redeem_return_rate", 0.5)
        compensation = cost * redeem_rate
        employer_data["coins"] += compensation

        await self._save_yaml_async(self.sign_data, DATA_FILE)

        employer_name = await self._get_user_name_from_platform(event, employer_id)
        yield event.plain_result(
            f"赎身成功，消耗{cost:.1f}金币，重获自由！"
            f"原雇主 {employer_name} 获得了 {compensation:.1f} 金币作为补偿。"
        )

    @filter.regex(r"^(我的信息|签到查询|我的资产)$")
    async def sign_query(self, event: AstrMessageEvent):
        html_url = await self._generate_card_html(event, is_query=True)
        if html_url:
            yield event.image_result(html_url)
        else:
            yield event.plain_result("查询失败，图片生成服务出现问题。")

    @filter.regex(r"^(存款|存钱)\s+([0-9.]+)$")
    async def deposit(self, event: AstrMessageEvent, amount_str: str):
        try:
            amount = float(amount_str)
            if amount <= 0:
                yield event.plain_result("存款金额必须大于0。")
                return
        except ValueError:
            yield event.plain_result("金额格式不正确，请使用：存款 <数字>")
            return
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user_data = self._get_user_data(self.sign_data, group_id, user_id)
        if amount > user_data["coins"]:
            yield event.plain_result(f"现金不足，当前现金：{user_data['coins']:.1f}")
            return
        user_data["coins"] -= amount
        user_data["bank"] += amount
        await self._save_yaml_async(self.sign_data, DATA_FILE)
        yield event.plain_result(f"成功存入 {amount:.1f} 金币到银行。")

    @filter.regex(r"^(取款|取钱)\s+([0-9.]+)$")
    async def withdraw(self, event: AstrMessageEvent, amount_str: str):
        try:
            amount = float(amount_str)
            if amount <= 0:
                yield event.plain_result("取款金额必须大于0。")
                return
        except ValueError:
            yield event.plain_result("金额格式不正确，请使用：取款 <数字>")
            return
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user_data = self._get_user_data(self.sign_data, group_id, user_id)
        if amount > user_data["bank"]:
            yield event.plain_result(f"银行存款不足，当前存款：{user_data['bank']:.1f}")
            return
        user_data["bank"] -= amount
        user_data["coins"] += amount
        await self._save_yaml_async(self.sign_data, DATA_FILE)
        yield event.plain_result(f"成功取出 {amount:.1f} 金币。")

    async def terminate(self):
        await self.session.close()

    async def _load_yaml_async(self, file_path: str) -> dict:
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                content = await f.read()
                return yaml.safe_load(content) or {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.error(f"异步加载YAML文件失败 ({file_path}): {e}")
            return {}

    async def _save_yaml_async(self, data: dict, file_path: str):
        try:
            async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                content = yaml.dump(data, allow_unicode=True)
                await f.write(content)
        except Exception as e:
            logger.error(f"异步保存YAML文件失败 ({file_path}): {e}")

    async def _load_all_data_to_cache(self):
        self.sign_data = await self._load_yaml_async(DATA_FILE)
        self.purchase_data = await self._load_yaml_async(PURCHASE_DATA_FILE)
        logger.info("签到插件数据已加载到缓存。")

    def _init_env(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(DATA_FILE):
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                yaml.dump({}, f)
        if not os.path.exists(PURCHASE_DATA_FILE):
            with open(PURCHASE_DATA_FILE, "w", encoding="utf-8") as f:
                yaml.dump({}, f)
        if not os.path.exists(self.font_path):
            logger.warning(f"字体文件缺失: {self.font_path}")
        if not os.path.exists(self.template_path):
            logger.error(f"HTML模板文件缺失: {self.template_path}")
        if not os.path.exists(self.default_bg_path):
            logger.warning(f"备用背景图文件缺失: {self.default_bg_path}")

    def _get_user_data(self, data_cache: dict, group_id: str, user_id: str) -> dict:
        return data_cache.setdefault(str(group_id), {}).setdefault(
            str(user_id),
            {
                "coins": 0.0,
                "bank": 0.0,
                "contractors": [],
                "contracted_by": None,
                "last_sign": None,
                "consecutive": 0,
            },
        )

    def _get_wealth_info(self, user_data: dict) -> tuple:
        total = user_data.get("coins", 0.0) + user_data.get("bank", 0.0)
        for min_coin, name, rate in reversed(WEALTH_LEVELS):
            if total >= min_coin:
                return name, rate
        return "平民", 0.25

    def _calculate_dynamic_wealth_value(
        self, user_data: dict, purchase_counts: dict, user_id: str
    ) -> float:
        total = user_data.get("coins", 0.0) + user_data.get("bank", 0.0)
        base_value = WEALTH_BASE_VALUES["平民"]
        for min_coin, name, _ in reversed(WEALTH_LEVELS):
            if total >= min_coin:
                base_value = WEALTH_BASE_VALUES[name]
                break
        contract_level = purchase_counts.get(str(user_id), 0)
        price_bonus = self.config.get("contract_level_price_bonus", 0.15)
        return base_value * (1 + contract_level * price_bonus)

    def _get_total_contractor_rate(self, group_id: str, contractor_ids: list) -> float:
        total_rate = 0.0
        rate_bonus = self.config.get("contract_level_rate_bonus", 0.075)
        for contractor_id in contractor_ids:
            contractor_data = self._get_user_data(
                self.sign_data, group_id, contractor_id
            )
            _, base_rate = self._get_wealth_info(contractor_data)
            contract_level = self.purchase_data.get(contractor_id, 0)
            total_rate += base_rate + (contract_level * rate_bonus)
        return total_rate

    async def _get_user_name_from_platform(
        self, event: AstrMessageEvent, target_id: str
    ) -> str:
        if event.get_platform_name() == "aiocqhttp":
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    resp = await client.api.call_action(
                        "get_group_member_info",
                        group_id=event.message_obj.group_id,
                        user_id=int(target_id),
                        no_cache=True,
                    )
                    return resp.get("card") or resp.get(
                        "nickname", f"用户{target_id[-4:]}"
                    )
            except Exception as e:
                logger.warning(f"通过API获取用户信息({target_id})失败: {e}")
        return f"用户{target_id[-4:]}"

    async def _image_to_base64(self, url: str) -> str:
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    image_bytes = await response.read()
                    encoded_string = base64.b64encode(image_bytes).decode("utf-8")
                    return f"data:{response.headers.get('Content-Type', 'image/jpeg')};base64,{encoded_string}"
                else:
                    logger.error(f"下载图片失败 ({url})，状态码: {response.status}")
                    return ""
        except Exception as e:
            logger.error(f"下载或转换图片时发生异常 ({url}): {e}")
            return ""

    def _file_to_base64(self, file_path: str) -> str:
        if not os.path.exists(file_path):
            return ""
        try:
            with open(file_path, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
                return f"data:image/jpeg;base64,{encoded_string}"
        except Exception as e:
            logger.error(f"读取本地图片文件失败 ({file_path}): {e}")
            return ""

    async def _generate_card_html(
        self,
        event: AstrMessageEvent,
        is_query: bool,
        is_penalized: bool = False,
        original_earned: float = 0.0,
    ) -> str:
        bg_api_url = self.config.get("bg_api_url", "https://t.alcy.cc/ycy")
        bg_image_data = await self._image_to_base64(bg_api_url)
        if not bg_image_data:
            bg_image_data = self._file_to_base64(self.default_bg_path)

        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user_data = self._get_user_data(self.sign_data, group_id, user_id)
        avatar_data = await self._image_to_base64(AVATAR_API.format(user_id))
        font_path = (
            f"file://{os.path.abspath(self.font_path)}"
            if os.path.exists(self.font_path)
            else ""
        )
        wealth_level, user_base_rate = self._get_wealth_info(user_data)

        render_data = {
            "font_path": font_path,
            "bg_image_data": bg_image_data,
            "avatar_data": avatar_data,
            "user_id": user_id,
            "user_name": event.get_sender_name(),
            "status": "受雇" if user_data["contracted_by"] else "自由",
            "wealth_level": wealth_level,
            "time_title": "查询时间" if is_query else "签到时间",
            "current_time": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "income_title": "明日预计收入" if is_query else "今日总收益",
            "coins": user_data["coins"],
            "bank": user_data["bank"],
            "consecutive": user_data["consecutive"],
            "is_query": is_query,
            "is_penalized": is_penalized,
            "original_earned": original_earned,
        }

        if is_query:
            names = [
                await self._get_user_name_from_platform(event, uid)
                for uid in user_data["contractors"]
            ]
            render_data["contractors_display"] = ", ".join(names) if names else "无"
            base_with_bonus = BASE_INCOME * (1 + user_base_rate)
            contractor_dynamic_rates = self._get_total_contractor_rate(
                group_id, user_data["contractors"]
            )
            contract_bonus = base_with_bonus * contractor_dynamic_rates
            consecutive_bonus = 10 * user_data["consecutive"]
            tomorrow_interest = user_data["bank"] * 0.01
            render_data.update(
                {
                    "total_income": base_with_bonus
                    + contract_bonus
                    + consecutive_bonus
                    + tomorrow_interest,
                    "base_with_bonus": base_with_bonus,
                    "contract_bonus": contract_bonus,
                    "consecutive_bonus": consecutive_bonus,
                    "tomorrow_interest": tomorrow_interest,
                }
            )
        else:
            render_data["contractors_display"] = str(len(user_data["contractors"]))
            interest = user_data["bank"] * 0.01
            earned = original_earned
            if is_penalized:
                income_rate = self.config.get("employed_income_rate", 0.7)
                earned *= income_rate
            render_data.update({"earned": earned + interest, "interest": interest})

        try:
            return await self.html_render(self.html_template, render_data)
        except Exception as e:
            logger.error(f"HTML 渲染失败: {e}")
            return ""

    def _load_template(self) -> str:
        if os.path.exists(self.template_path):
            try:
                with open(self.template_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.error(f"读取HTML模板文件失败: {e}")
        return "<h1>模板文件加载失败</h1>"
