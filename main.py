from astrbot.api.all import *
from astrbot.api.message_components import Image, Plain
from datetime import datetime, timedelta
import yaml
import os
import requests
import pytz
import re
from PIL import ImageColor, Image as PILImage
from PIL import ImageDraw, ImageFont, ImageOps
from io import BytesIO
from typing import Dict, Any

# 路径配置
PLUGIN_DIR = os.path.join('data', 'plugins', 'astrbot_plugin_Qsign')
DATA_FILE = os.path.join('data', 'sign_data.yml')
IMAGE_DIR = os.path.join(PLUGIN_DIR, 'images')
FONT_PATH = os.path.join(PLUGIN_DIR, '请以你的名字呼唤我.ttf')

# API配置
AVATAR_API = "http://q.qlogo.cn/headimg_dl?dst_uin={}&spec=640&img_type=jpg"
BG_API = "https://api.fuchenboke.cn/api/dongman.php"
# 经济系统配置
WEALTH_LEVELS = [
    (0,    "平民", 0.25),
    (500,  "小资", 0.5),
    (2000, "富豪", 0.75),
    (5000, "巨擘", 1.0)
]
WEALTH_BASE_VALUES = {
    "平民": 100,
    "小资": 500,
    "富豪": 2000,
    "巨擘": 5000
}
BASE_INCOME = 100.0

# 时区配置
SHANGHAI_TZ = pytz.timezone('Asia/Shanghai')

@register("astrbot_plugin_sign", "长安某", "签到前置", "1.1", "https://github.com/zgojin/astrbot_plugin_sign")
class ContractSystem(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._init_env()

    def _init_env(self):
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        os.makedirs(PLUGIN_DIR, exist_ok=True)
        os.makedirs(IMAGE_DIR, exist_ok=True)
        if not os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                yaml.dump({}, f)
        if not os.path.exists(FONT_PATH):
            raise FileNotFoundError(f"字体文件缺失: {FONT_PATH}")

    def _load_data(self):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception:
            # 静默失败，返回空数据
            return {}

    def _save_data(self, data):
        try:
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, allow_unicode=True)
        except Exception:
            pass  # 静默保存失败

    def _get_user_data(self, group_id: str, user_id: str) -> dict:
        # 每次获取用户数据时重新加载 YAML 文件
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {}

        user_data = data.setdefault(group_id, {}).setdefault(user_id, {
            "coins": 0.0,
            "bank": 0.0,
            "contractors": [],
            "contracted_by": None,
            "last_sign": None,
            "consecutive": 0
        })
        
        # 每次获取用户数据时重新读取牛牛插件的金币数据
        niuniu_data_path = os.path.join('data', 'niuniu_lengths.yml')
        if os.path.exists(niuniu_data_path):
            try:
                with open(niuniu_data_path, 'r', encoding='utf-8') as f:
                    niuniu_data = yaml.safe_load(f) or {}
                # 牛牛插件的数据结构为 niuniu_data[group_id][user_id]['coins']
                niuniu_coins = niuniu_data.get(group_id, {}).get(user_id, {}).get('coins', 0.0)
                user_data['niuniu_coins'] = niuniu_coins
            except Exception:
                user_data['niuniu_coins'] = 0.0
        else:
            user_data['niuniu_coins'] = 0.0
        
        return user_data

    def _get_wealth_info(self, user_data: dict) -> tuple:
        total = user_data["coins"] + user_data.get("niuniu_coins", 0.0) + user_data["bank"]
        for min_coin, name, rate in reversed(WEALTH_LEVELS):
            if total >= min_coin:
                return (name, rate)
        return ("平民", 0.25)

    def _calculate_wealth(self, user_data: dict) -> float:
        level_name, _ = self._get_wealth_info(user_data)
        return WEALTH_BASE_VALUES.get(level_name, 100)

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        if msg.startswith("购买"):
            target_id = self._parse_at_target(event)
            if not target_id:
                yield event.plain_result("❌ 请@要购买的对象")
                return
            async for result in self._handle_hire(event, group_id, user_id, target_id):
                yield result
            return

        elif msg.startswith("出售"):
            target_id = self._parse_at_target(event)
            if not target_id:
                yield event.plain_result("❌ 请@要出售的对象")
                return
            async for result in self._handle_sell(event, group_id, user_id, target_id):
                yield result
            return

    def _parse_at_target(self, event):
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                return str(comp.qq)
        return None

    async def _handle_hire(self, event, group_id, employer_id, target_id):
        employer = self._get_user_data(group_id, employer_id)
        target_user = self._get_user_data(group_id, target_id)
        
        if not target_user["last_sign"]:
            target_name = await self._get_at_user_name(event, target_id)
            yield event.plain_result(f"❌ {target_name} 尚未签到，不可购买")
            return
        
        if len(employer["contractors"]) >= 3:
            yield event.plain_result("❌ 已达最大购买数量（3人）")
            return
        
        cost = self._calculate_wealth(target_user)
        if employer["coins"] < cost:
            yield event.plain_result(f"❌ 需要支付目标身价：{cost}金币")
            return

        employer["coins"] -= cost
        employer["contractors"].append(target_id)
        target_user["contracted_by"] = employer_id
        
        # 保存数据
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            group_data = data.setdefault(group_id, {})
            group_data[employer_id] = employer
            group_data[target_id] = target_user
            self._save_data(data)
        except Exception:
            pass
        
        target_name = await self._get_at_user_name(event, target_id)
        yield event.plain_result(f"✅ 成功购买 {target_name}，消耗{cost}金币")

    async def _handle_sell(self, event, group_id, employer_id, target_id):
        employer = self._get_user_data(group_id, employer_id)
        target_user = self._get_user_data(group_id, target_id)

        if not target_user["last_sign"]:
            target_name = await self._get_at_user_name(event, target_id)
            yield event.plain_result(f"❌ {target_name} 尚未签到，不可出售")
            return

        if target_id not in employer["contractors"]:
            yield event.plain_result("❌ 目标不在你的黑奴列表中")
            return

        sell_price = self._calculate_wealth(target_user) * 0.2
        employer["coins"] += sell_price
        employer["contractors"].remove(target_id)
        target_user["contracted_by"] = None
        
        # 保存数据
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            group_data = data.setdefault(group_id, {})
            group_data[employer_id] = employer
            group_data[target_id] = target_user
            self._save_data(data)
        except Exception:
            pass
        
        target_name = await self._get_at_user_name(event, target_id)
        yield event.plain_result(f"✅ 成功出售黑奴，获得{sell_price:.1f}金币")

    async def _get_at_user_name(self, event, target_id: str) -> str:
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            if isinstance(event, AiocqhttpMessageEvent):
                client = event.bot
                resp = await client.api.call_action(
                    'get_group_member_info',
                    group_id=event.message_obj.group_id,
                    user_id=int(target_id),
                    no_cache=True
                )
                return resp.get('card') or resp.get('nickname', f'用户{target_id[-4:]}')
                
            raw_msg = event.message_str
            if match := re.search(r'\$CQ:at,qq=(\d+)\$', raw_msg):
                return f'用户{match.group(1)[-4:]}'
            return f'用户{target_id[-4:]}'
        except Exception:
            return "神秘用户"

    @command("赎身")
    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def terminate_contract(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user_data = self._get_user_data(group_id, user_id)
        
        if not user_data["contracted_by"]:
            yield event.chain_result([Plain(text="❌ 您暂无契约在身")])
            return

        cost = self._calculate_wealth(user_data)
        if user_data["coins"] < cost:
            yield event.chain_result([Plain(text=f"❌ 需要支付赎身费用：{cost:.1f}金币")])
            return

        employer_id = user_data["contracted_by"]
        employer = self._get_user_data(group_id, employer_id)
        if user_id in employer["contractors"]:
            employer["contractors"].remove(user_id)
        
        user_data["contracted_by"] = None
        user_data["coins"] -= cost
        
        # 保存数据
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            group_data = data.setdefault(group_id, {})
            group_data[user_id] = user_data
            group_data[employer_id] = employer
            self._save_data(data)
        except Exception:
            pass
        
        yield event.chain_result([Plain(text=f"✅ 赎身成功，消耗{cost:.1f}金币")])

    @command("存款")
    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def deposit(self, event: AstrMessageEvent):
        msg_parts = event.message_str.strip().split()
        if len(msg_parts) < 2:
            yield event.chain_result([Plain(text="❌ 格式错误，请使用：/存款 <金额>")])
            return
        
        try:
            amount = float(msg_parts[1])
        except ValueError:
            yield event.chain_result([Plain(text="❌ 请输入有效的数字金额")])
            return

        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user_data = self._get_user_data(group_id, user_id)
        
        if amount <= 0:
            yield event.chain_result([Plain(text="❌ 存款金额必须大于0")])
            return
        
        # 计算可用总额（本插件金币 + 牛牛插件金币）
        total_available = user_data["coins"] + user_data.get("niuniu_coins", 0.0)
        
        if amount > total_available:
            yield event.chain_result([Plain(text="❌ 可用金币不足")])
            return
        
        # 优先使用本插件的金币
        if user_data["coins"] >= amount:
            user_data["coins"] -= amount
        else:
            remaining = amount - user_data["coins"]
            user_data["coins"] = 0.0
            # 从牛牛插件的金币中扣除剩余部分
            niuniu_data_path = os.path.join('data', 'niuniu_lengths.yml')
            if os.path.exists(niuniu_data_path):
                try:
                    with open(niuniu_data_path, 'r', encoding='utf-8') as f:
                        niuniu_data = yaml.safe_load(f) or {}
                    # 确保群组和用户数据存在
                    if group_id not in niuniu_data:
                        niuniu_data[group_id] = {}
                    if user_id not in niuniu_data[group_id]:
                        niuniu_data[group_id][user_id] = {}
                    niuniu_data[group_id][user_id]['coins'] = niuniu_data[group_id][user_id].get('coins', 0.0) - remaining
                    with open(niuniu_data_path, 'w', encoding='utf-8') as f:
                        yaml.dump(niuniu_data, f, allow_unicode=True)
                except Exception:
                    yield event.chain_result([Plain(text="❌ 更新牛牛插件数据失败")])
                    return
        
        user_data["bank"] += amount
        
        # 保存数据
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            group_data = data.setdefault(group_id, {})
            group_data[user_id] = user_data
            self._save_data(data)
        except Exception:
            pass
        
        yield event.chain_result([Plain(text=f"✅ 成功存入 {amount:.1f} 金币")])

    @command("取款")
    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def withdraw(self, event: AstrMessageEvent):
        msg_parts = event.message_str.strip().split()
        if len(msg_parts) < 2:
            yield event.chain_result([Plain(text="❌ 格式错误，请使用：/取款 <金额>")])
            return
        
        try:
            amount = float(msg_parts[1])
        except ValueError:
            yield event.chain_result([Plain(text="❌ 请输入有效的数字金额")])
            return

        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user_data = self._get_user_data(group_id, user_id)
        
        if amount <= 0:
            yield event.chain_result([Plain(text="❌ 取款金额必须大于0")])
            return
        
        if amount > user_data["bank"]:
            yield event.chain_result([Plain(text="❌ 银行存款不足")])
            return
        
        user_data["bank"] -= amount
        user_data["coins"] += amount
        
        # 保存数据
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            group_data = data.setdefault(group_id, {})
            group_data[user_id] = user_data
            self._save_data(data)
        except Exception:
            pass
        
        yield event.chain_result([Plain(text=f"✅ 成功取出 {amount:.1f} 金币")])

    @command("签到")
    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def sign_in(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user_data = self._get_user_data(group_id, user_id)
        
        now = datetime.now(SHANGHAI_TZ)
        today = now.date()
        
        if user_data["last_sign"]:
            last_sign = SHANGHAI_TZ.localize(datetime.fromisoformat(user_data["last_sign"]))
            if last_sign.date() == today:
                yield event.chain_result([Plain(text="❌ 今日已签到，请明天再来！")])
                return

        interest = user_data["bank"] * 0.01
        user_data["bank"] += interest

        if user_data["last_sign"]:
            last_sign = SHANGHAI_TZ.localize(datetime.fromisoformat(user_data["last_sign"]))
            delta = today - last_sign.date()
            user_data["consecutive"] = 1 if delta.days > 1 else user_data["consecutive"] + 1
        else:
            user_data["consecutive"] = 1

        # 获取用户自身身份的加成
        user_wealth_level, user_wealth_rate = self._get_wealth_info(user_data)
        
        # 计算契约收益加成
        contractor_rates = sum(
            self._get_wealth_info(self._get_user_data(group_id, c))[1]
            for c in user_data["contractors"]
        )
        
        # 计算连签奖励
        consecutive_bonus = 10 * (user_data["consecutive"] - 1)  
        
        # 计算签到收益
        earned = BASE_INCOME * (1 + user_wealth_rate) * (1 + contractor_rates) + consecutive_bonus

        user_data["coins"] += earned
        user_data["last_sign"] = now.replace(tzinfo=None).isoformat()
        
        # 保存数据
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            group_data = data.setdefault(group_id, {})
            group_data[user_id] = user_data
            self._save_data(data)
        except Exception:
            pass

        card_path = await self._generate_card(
            event=event,
            user_id=user_id,
            user_name=event.get_sender_name(),
            coins=user_data["coins"] + user_data.get("niuniu_coins", 0.0),  # 显示总额
            bank=user_data["bank"],
            consecutive=user_data["consecutive"],
            contractors=user_data["contractors"],
            is_contracted=bool(user_data["contracted_by"]),
            interest=interest,
            earned=earned,
            group_id=group_id,
            is_query=False
        )
        yield event.chain_result([Image.fromFileSystem(card_path)])
    
    @command("签到查询")
    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def sign_query(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user_data = self._get_user_data(group_id, user_id)
        
        # 获取用户自身身份的加成
        user_wealth_level, user_wealth_rate = self._get_wealth_info(user_data)
        
        # 计算契约收益加成
        contractor_rates = sum(
            self._get_wealth_info(self._get_user_data(group_id, c))[1]
            for c in user_data["contractors"]
        )
        
        # 计算连签奖励
        consecutive_bonus = 10 * user_data["consecutive"]
        
        # 计算预期收益
        earned = BASE_INCOME * (1 + user_wealth_rate) * (1 + contractor_rates) + consecutive_bonus

        card_path = await self._generate_card(
            event=event,
            user_id=user_id,
            user_name=event.get_sender_name(),
            coins=user_data["coins"] + user_data.get("niuniu_coins", 0.0),  # 显示总额
            bank=user_data["bank"],
            consecutive=user_data["consecutive"],
            contractors=user_data["contractors"],
            is_contracted=bool(user_data["contracted_by"]),
            interest=user_data["bank"] * 0.01,
            earned=earned,
            group_id=group_id,
            is_query=True,
            user_wealth_rate=user_wealth_rate
        )
        yield event.chain_result([Image.fromFileSystem(card_path)])

    async def _generate_card(self, **data):

        try:
            bg_response = requests.get(BG_API, timeout=10)
            bg = PILImage.open(BytesIO(bg_response.content)).resize((1080, 720))
        except Exception:
            bg = PILImage.new("RGB", (1080, 720), color="#FFFFFF")

        def create_rounded_panel(size, color):
            panel = PILImage.new("RGBA", size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(panel)
            draw.rounded_rectangle([(0, 0), (size[0]-1, size[1]-1)], radius=20, fill=color)
            return panel

        canvas = PILImage.new("RGBA", bg.size)
        canvas.paste(bg, (0, 0))
        draw = ImageDraw.Draw(canvas)
        avatar_y = 200
        info_start_y = 230
        # 头像处理
        avatar = await self._get_avatar(data["user_id"])
        if avatar:
            canvas.paste(avatar, (60, avatar_y), avatar)

        # 基础信息
        info_font = ImageFont.truetype(FONT_PATH, 28)
        name_font = ImageFont.truetype(FONT_PATH, 36)
        
        draw.text(
        (260, info_start_y), 
        f"QQ：{data['user_id']}", 
        font=info_font, 
        fill="#000000",
        stroke_width=1,      
        stroke_fill="#FFFFFF"
    )
        draw.text(
        (260, info_start_y + 40), 
        data["user_name"], 
        font=name_font, 
        fill="#FFA500",
        stroke_width=1,
        stroke_fill="#000000"
    )
        
        status = "黑奴" if data["is_contracted"] else "自由民"
        wealth_level, _ = self._get_wealth_info({
            "coins": data["coins"], 
            "bank": data["bank"]
        })
        draw.text(
            (260, info_start_y + 80),
            f"身份：{status} | 等级：{wealth_level}", 
            font=info_font, 
            fill="#333333",
            stroke_width=1,       
            stroke_fill="#FFFFFF"  
        )

        # 左侧时间面板
        PANEL_WIDTH = 510
        PANEL_HEIGHT = 120
        SIDE_MARGIN = 20
        panel_y = 400

        left_panel = create_rounded_panel((PANEL_WIDTH, PANEL_HEIGHT), (255,255,255,150))
        canvas.paste(left_panel, (SIDE_MARGIN, panel_y), left_panel)
        
        time_font = ImageFont.truetype(FONT_PATH, 28)
        time_title = "查询时间" if data.get('is_query') else "签到时间"
        draw.text((SIDE_MARGIN+20, panel_y+20), time_title, font=time_font, fill="#333333")
        
        current_time = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
        draw.text((SIDE_MARGIN+20, panel_y+60), current_time, font=time_font, fill="#333333")

        # 右侧收益面板
        right_panel_x = SIDE_MARGIN + PANEL_WIDTH + 20
        right_panel = create_rounded_panel((PANEL_WIDTH, PANEL_HEIGHT), (255,255,255,150))
        canvas.paste(right_panel, (right_panel_x, panel_y), right_panel)
        
        title_font = ImageFont.truetype(FONT_PATH, 32)
        title_text = "预计收入" if data.get('is_query') else "今日收益"
        draw.text((right_panel_x+20, panel_y+20), title_text, font=title_font, fill="#333333")

        detail_font = ImageFont.truetype(FONT_PATH, 24)
        line_height = 35
        
        if data.get('is_query'):
            # 计算加成后的基础收益
            base_with_bonus = BASE_INCOME * (1 + data['user_wealth_rate'])
            contract_bonus = sum(
                self._get_wealth_info(
                    self._get_user_data(data['group_id'], c)
                )[1] * base_with_bonus  # 使用加成后的基础收益计算契约加成
                for c in data['contractors']
            )
            consecutive_bonus = 10 * data['consecutive']  # 显示明日可得的连签奖励
            tomorrow_interest = data["bank"] * 0.01
            
            total = base_with_bonus + contract_bonus + consecutive_bonus + tomorrow_interest
            lines = [
                f"{total:.1f} 金币",
                f"基础{base_with_bonus:.1f}+契约{contract_bonus:.1f}+连签{consecutive_bonus:.1f}+利息{tomorrow_interest:.1f}"
            ]
        else:
            lines = [f"{data['earned']:.1f}（含利息{data['interest']:.1f}）"]
        start_y = panel_y + 50
        for i, line in enumerate(lines):
            text_bbox = detail_font.getbbox(line)
            text_width = text_bbox[2] - text_bbox[0]
            
            y_position = start_y + i*line_height
            if i == 0:
                draw.text(
                    (right_panel_x + PANEL_WIDTH//2 - text_width//2, y_position),
                    line,
                    font=ImageFont.truetype(FONT_PATH, 28),
                    fill="#FF4500"
                )
            else:
                draw.text(
                    (right_panel_x + PANEL_WIDTH//2 - text_width//2, y_position),
                    line,
                    font=detail_font,
                    fill="#333333"
                )

        # 底部数据面板
        BOTTOM_HEIGHT = 150
        BOTTOM_TOP = 720 - BOTTOM_HEIGHT - 20
        bottom_panel = create_rounded_panel((1040, BOTTOM_HEIGHT), (255,255,255,150))
        canvas.paste(bottom_panel, (20, BOTTOM_TOP), bottom_panel)

        # 获取黑奴名称
        contractors_display = ""
        if data.get('is_query'):
            names = []
            for uid in data['contractors']:
                try:
                    name = await self._get_at_user_name(data['event'], uid)
                    name = name.replace('用户', '') 
                    names.append(name)
                except:
                    names.append("未知")
            contractors_display = ','.join(names) if names else "无"
        else:
            contractors_display = str(len(data['contractors']))

        metrics = [
            ("现金", f"{data['coins']:.1f}", 60),
            ("银行", f"{data['bank']:.1f}", 300),
            ("黑奴", contractors_display, 560),
            ("连续签到", str(data['consecutive']), 820)
        ]
        
        # 绘制指标
        for title, value, x in metrics:
            # 标题
            draw.text(
                (x, BOTTOM_TOP+30), 
                title, 
                font=ImageFont.truetype(FONT_PATH, 28), 
                fill="#333333"
            )
            
            if title == "黑奴" and data.get('is_query'):
                max_line_width = 200  # 每行最大宽度
                line_spacing = 35     # 行间距
                current_y = BOTTOM_TOP + 70
                current_line = []
                
                for name in value.split(','):
                    # 截断超长名字
                    display_name = f"{name[:6]}.." if len(name) > 6 else name
                    test_line = current_line + [display_name]
                    test_text = ','.join(test_line)
                    bbox = ImageFont.truetype(FONT_PATH,28).getbbox(test_text)
                    text_width = bbox[2] - bbox[0]
                    
                    if text_width > max_line_width:
                        draw.text(
                            (x, current_y), 
                            ','.join(current_line),
                            font=ImageFont.truetype(FONT_PATH,28), 
                            fill="#000000"
                        )
                        current_line = [display_name]
                        current_y += line_spacing
                    else:
                        current_line.append(display_name)
                if current_line:
                    draw.text(
                        (x, current_y), 
                        ','.join(current_line),
                        font=ImageFont.truetype(FONT_PATH,28), 
                        fill="#000000"
                    )
            else:
                draw.text(
                    (x, BOTTOM_TOP+80), 
                    value, 
                    font=ImageFont.truetype(FONT_PATH,28), 
                    fill="#000000"
                )
        copyright_font = ImageFont.truetype(FONT_PATH, 24)
        copyright_text = "by长安某"
        text_bbox = copyright_font.getbbox(copyright_text)
        draw.text(
            (1080 - text_bbox[2] - 20, 720 - text_bbox[3] - 20),
            copyright_text,
            font=copyright_font,
            fill="#666666"
        )

        # 保存图片
        filename = f"sign_{data['user_id']}.png"
        save_path = os.path.join(IMAGE_DIR, filename)
        canvas.save(save_path)
        
        return save_path

    async def _get_avatar(self, user_id: str):
        try:
            response = requests.get(AVATAR_API.format(user_id), timeout=5)
            img = PILImage.open(BytesIO(response.content))
            
            mask = PILImage.new('L', (160, 160), 0)
            draw = ImageDraw.Draw(mask)
            draw.ellipse((0, 0, 160, 160), fill=255)
            
            bordered = PILImage.new("RGBA", (166, 166), (255,255,255,0))
            bordered.paste(img.resize((160,160)), (3,3), mask)
            return bordered
        except Exception:
            return None
