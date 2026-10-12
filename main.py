import asyncio
import logging
import os
import pytz
import gspread
from datetime import datetime
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InputMediaPhoto,
    FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================= НАСТРОЙКИ =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)


# --- Настройка Google Таблиц ---
def setup_google_sheets():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name("freshtouch-service_account.json", scope)
        client = gspread.authorize(creds)
        spreadsheet = client.open("FreshTouch_Заявки")
        return spreadsheet
    except Exception as e:
        logger.error(f"Ошибка подключения к Google Таблицам: {e}")
        return None


gs_client = setup_google_sheets()
orders_sheet = gs_client.worksheet("Заказы") if gs_client else None
users_sheet = gs_client.worksheet("Пользователи") if gs_client else None

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ================= ХРАНИЛИЩЕ ПОЛЬЗОВАТЕЛЕЙ =================
# { user_id: { "name": str, "phone": str, "username": str } }
users_db: dict[int, dict] = {}


def sync_users_from_table():
    if not users_sheet:
        logger.error("❌ ЛИСТ ПОЛЬЗОВАТЕЛЕЙ НЕ НАЙДЕН! Проверьте название листа в коде и в таблице.")
        return
    try:
        rows = users_sheet.get_all_values()
        logger.info(f"📊 Всего строк на листе 'Пользователи' (включая заголовки): {len(rows)}")

        if len(rows) > 1:
            # Начинаем со второй строки (индекс 1)
            for idx, row in enumerate(rows[1:], start=2):
                if not row or not row[0]:
                    continue

                try:
                    # Очищаем ID от случайных пробелов, которые мог добавить Google
                    clean_id = str(row[0]).replace(" ", "").strip()
                    user_id = int(clean_id)

                    users_db[user_id] = {
                        "name": str(row[1]).strip() if len(row) > 1 else "",
                        "phone": str(row[2]).strip() if len(row) > 2 else "",
                        "username": str(row[3]).strip() if len(row) > 3 else ""
                    }
                except ValueError:
                    logger.error(f"⚠️ ОШИБКА В СТРОКЕ {idx}: Невозможно прочитать ID '{row[0]}'. Исправьте в таблице!")
                    continue

        logger.info(f"✅ Успешно загружено пользователей в память бота: {len(users_db)}")
    except Exception as e:
        logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА синхронизации: {e}")

def user_has_orders(user_id: int) -> bool:
    """Проверяем, есть ли у пользователя заказы в листе Заказы по второй колонке."""
    if not orders_sheet:
        return False
    try:
        # Берем все значения строго из 2-й колонки (где мы сохраняем user.id)
        # col_values(2) работает намного быстрее и не зависит от названий заголовков
        user_ids = orders_sheet.col_values(2)

        # Если ID пользователя уже есть в этом столбце, значит он делал заказ
        if str(user_id) in user_ids:
            return True

        return False
    except Exception as e:
        logger.error(f"Ошибка проверки заказов: {e}")
        return False


# ================= FSM =================
class Registration(StatesGroup):
    entering_name = State()
    entering_phone = State()


class CleanOrder(StatesGroup):
    choosing_item = State()
    choosing_sofa_type = State()
    choosing_mattress_size = State()
    choosing_side = State()
    entering_quantity = State()
    choosing_dirt = State()
    uploading_photos = State()
    entering_comment = State()
    entering_area = State()
    entering_custom = State()
    choosing_datetime = State()
    confirming_phone = State()
    entering_new_phone = State()
    entering_address = State()


# ================= ЦЕНЫ =================
SOFA_PRICES = {
    "Прямой": 3000,
    "Угловой": 3500,
    "Длинный прямой": 3500,
    "Длинный угловой": 4000,
    "Модульный": 4500,
    "П-образный": 4500,
}

MATTRESS_PRICES = {
    "80/90 см": 1800,
    "120 см": 2500,
    "140 см": 3000,
    "160 см": 3500,
    "180 см": 4000,
    "200 см": 4500,
}

BASE_PRICES = {
    "Кресло": 1500,
    "Стул": 550,
    "Другое": 1200,
}

DIRT_MULTIPLIER = {
    "Лёгкая": 1.0,
    "Средняя": 1.2,
    "Сильная": 1.5,
}

SOFA_TYPES = list(SOFA_PRICES.keys())
MATTRESS_SIZES = list(MATTRESS_PRICES.keys())


# ================= КЛАВИАТУРЫ =================
def cancel_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить заявку", callback_data="cancel")]]
    )


def order_again_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🧼 Заказать чистку", callback_data="start_calc")]]
    )


def skip_photo_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить фото", callback_data="skip_photo")],
            [InlineKeyboardButton(text="❌ Отменить заявку", callback_data="cancel")],
        ]
    )


def main_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧼 Заказать чистку", callback_data="start_calc")],
            [InlineKeyboardButton(text="🎁 Акции и специальные предложения", callback_data="promotions")],
        ]
    )


def confirm_order_kb(has_items: bool = False):
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить ещё позицию", callback_data="add_more")],
        [InlineKeyboardButton(text="✅ Подтвердить заявку", callback_data="confirm")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ================= ОТМЕНА =================
@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel")
async def cancel_handler(event: types.Message | types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = "❌ Действие отменено."
    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=ReplyKeyboardRemove())
        await event.message.answer("Возвращаемся в главное меню 👇", reply_markup=main_menu_kb())
        await event.answer()
    else:
        await event.answer(text, reply_markup=ReplyKeyboardRemove())
        await event.answer("Возвращаемся в главное меню 👇", reply_markup=main_menu_kb())


# ================= HELP =================
@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "🤖 *FreshTouch Bot — помощь*\n\n"
        "/start — начать / главное меню\n"
        "/cancel — отменить текущую заявку\n"
        "/help — эта справка",
        parse_mode="Markdown"
    )


# ================= START =================
@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    if user_id in users_db:
        name = users_db[user_id]["name"]
        await message.answer(
            f"👋 С возвращением, *{name}*!\n"
            "Чем могу помочь?",
            reply_markup=main_menu_kb(),
            parse_mode="Markdown"
        )
        return

    await state.set_state(Registration.entering_name)
    await message.answer(
        "👋 Привет! Я — помощник химчистки *FreshTouch* в Краснодаре 🌿\n\n"
        "Давайте познакомимся. Как вас зовут?",
        parse_mode="Markdown"
    )


# ================= РЕГИСТРАЦИЯ: ИМЯ =================
@router.message(Registration.entering_name)
async def reg_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2 or len(name) > 50:
        await message.answer("❗ Введите корректное имя (от 2 до 50 символов)")
        return
    await state.update_data(name=name)

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await state.set_state(Registration.entering_phone)
    await message.answer(
        f"Приятно познакомиться, *{name}*! 😊\n\n"
        "📞 Укажите ваш номер телефона — нажмите кнопку ниже или введите вручную:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


# ================= РЕГИСТРАЦИЯ: ТЕЛЕФОН =================
@router.message(Registration.entering_phone)
async def reg_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text.strip()

    digits = phone.replace("+", "").replace(" ", "").replace("-", "")
    if not digits.isdigit() or len(digits) < 10:
        await message.answer("❗ Введите корректный номер телефона")
        return

    data = await state.get_data()
    name = data["name"]
    user_id = message.from_user.id
    username = message.from_user.username or ""

    users_db[user_id] = {
        "name": name,
        "phone": phone,
        "username": username,
    }

    if users_sheet:
        try:
            users_sheet.append_row([user_id, name, phone, username])
        except Exception as e:
            logger.error(f"Ошибка записи пользователя в таблицу: {e}")

    logger.info(f"Зарегистрирован пользователь {user_id}: {name}, {phone}")

    await state.clear()

    await message.answer(
        f"✅ Отлично, *{name}*! Вы зарегистрированы.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    await message.answer(
        "Чем могу помочь? Выберите действие 👇",
        reply_markup=main_menu_kb()
    )


# ================= АКЦИИ =================
@router.callback_query(F.data == "promotions")
async def promotions(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    has_orders = user_has_orders(user_id)

    if not has_orders:
        new_client_block = (
            "🎉 *Скидка 10% на первую чистку!*\n"
            "Для вас действует скидка 10% — она применится автоматически при оформлении заказа.\n\n"
        )
    else:
        new_client_block = ""

    text = (
            "🌟 *Акции и специальные предложения*\n\n"
            "📦 *Скидка за объём работ*\n"
            "При заказе от 3 позиций мебели — скидка *15%* на всю сумму!\n\n"
            + new_client_block +
            "🦠 *Антибактериальная чистка в подарок!*\n"
            "До конца 2026 года при любом заказе химчистки — антибактериальная обработка *бесплатно*.\n\n"
            "📚 *Мини-гайд по уходу за мебелью:*\n"
            "1️⃣ Не трите пятна горячей водой\n"
            "2️⃣ Используйте pH-нейтральные средства\n"
            "3️⃣ Промакивайте пятна, а не трите\n\n"
            "Готовы заказать чистку? 👇"
    )

    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🧼 Заказать чистку", callback_data="start_calc")],
                [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="main_menu")],
            ]
        ),
        parse_mode="Markdown",
    )
    await callback.answer()


# ================= ГЛАВНОЕ МЕНЮ =================
@router.callback_query(F.data == "main_menu")
async def main_menu_cb(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    name = users_db.get(user_id, {}).get("name", "")
    greeting = f"*{name}*, чем могу помочь?" if name else "Чем могу помочь?"
    await callback.message.answer(greeting, reply_markup=main_menu_kb(), parse_mode="Markdown")
    await callback.answer()


# ================= ШАГ 1: ВЫБОР ПРЕДМЕТА =================
async def show_item_choice(message: types.Message, state: FSMContext):
    builder = InlineKeyboardBuilder()
    for item in ["Диван", "Кресло", "Стул", "Матрас", "Ковёр", "Другое"]:
        builder.button(text=item, callback_data=f"item:{item}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data="cancel"))

    await state.set_state(CleanOrder.choosing_item)
    await message.answer(
        "🛋 *Что будем чистить?*",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "start_calc")
async def choose_item_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_item_choice(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "add_more")
async def add_more_item(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()

    items_list = data.get("items_list", [])
    current_item = _extract_current_item(data)
    if current_item:
        items_list.append(current_item)

    await state.set_data({
        "items_list": items_list,
        "address": data.get("address"),
        "datetime": data.get("datetime"),
        "phone": data.get("phone"),
        "item": None,
        "sofa_type": None,
        "mattress_size": None,
        "side": None,
        "quantity": None,
        "area": None,
        "custom": None,
        "dirt": None,
        "photos": [],
        "comment": None,
    })

    count = len(items_list)
    await callback.message.answer(
        f"✅ Позиция #{count} добавлена!\n\n"
        "Выберите следующий предмет для чистки:",
    )
    await show_item_choice(callback.message, state)
    await callback.answer()


def _extract_current_item(data: dict) -> dict | None:
    if not data.get("item"):
        return None
    return {
        "item": data.get("item"),
        "sofa_type": data.get("sofa_type"),
        "mattress_size": data.get("mattress_size"),
        "side": data.get("side", 1.0),
        "quantity": data.get("quantity", 1),
        "area": data.get("area"),
        "custom": data.get("custom"),
        "dirt": data.get("dirt", "Лёгкая"),
        "photos": data.get("photos", []),
        "comment": data.get("comment"),
        "price": calculate_item_price(data),
    }


# ================= ВЫБОР ПРЕДМЕТА (ЛОГИКА) =================
@router.callback_query(CleanOrder.choosing_item, F.data.startswith("item:"))
async def item_logic(callback: types.CallbackQuery, state: FSMContext):
    item = callback.data.split(":")[1]
    await state.update_data(item=item)

    if item == "Диван":
        await state.set_state(CleanOrder.choosing_sofa_type)
        await ask_sofa_type(callback.message)
    elif item in ["Кресло", "Стул"]:
        await state.set_state(CleanOrder.entering_quantity)
        await callback.message.answer("🔢 Укажите количество (например: 2)", reply_markup=cancel_kb())
    elif item == "Матрас":
        await state.set_state(CleanOrder.choosing_mattress_size)
        await ask_mattress_size(callback.message)
    elif item == "Ковёр":
        await state.set_state(CleanOrder.entering_area)
        await callback.message.answer("📐 Укажите площадь ковра в м² (например: 12.5)", reply_markup=cancel_kb())
    elif item == "Другое":
        await state.set_state(CleanOrder.entering_custom)
        await callback.message.answer("✍️ Опишите, что нужно почистить", reply_markup=cancel_kb())
    else:
        await state.set_state(CleanOrder.choosing_dirt)
        await ask_dirt(callback.message)

    await callback.answer()


# ================= ТИП ДИВАНА =================
async def ask_sofa_type(message: types.Message):
    builder = InlineKeyboardBuilder()
    for sofa_type in SOFA_TYPES:
        builder.button(text=sofa_type, callback_data=f"sofa:{sofa_type}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data="cancel"))

    image_path = "Виды диванов.png"
    caption = "🛋 *Выберите тип дивана*\n\nВыберите наиболее подходящий по виду диван 👇"
    if os.path.exists(image_path):
        photo = FSInputFile(image_path)
        await message.answer_photo(photo=photo, caption=caption, reply_markup=builder.as_markup(),
                                   parse_mode="Markdown")
    else:
        await message.answer(caption, reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(CleanOrder.choosing_sofa_type, F.data.startswith("sofa:"))
async def sofa_type_chosen(callback: types.CallbackQuery, state: FSMContext):
    sofa_type = callback.data.split(":", 1)[1]
    await state.update_data(sofa_type=sofa_type)
    await state.set_state(CleanOrder.choosing_dirt)
    await ask_dirt(callback.message)
    await callback.answer()


# ================= РАЗМЕР МАТРАСА =================
async def ask_mattress_size(message: types.Message):
    builder = InlineKeyboardBuilder()
    for size in MATTRESS_SIZES:
        builder.button(text=size, callback_data=f"msize:{size}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data="cancel"))
    await message.answer("🛏 *Выберите ширину матраса:*", reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(CleanOrder.choosing_mattress_size, F.data.startswith("msize:"))
async def mattress_size_chosen(callback: types.CallbackQuery, state: FSMContext):
    size = callback.data.split(":", 1)[1]
    await state.update_data(mattress_size=size)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Одна сторона", callback_data="side:1.0")],
        [InlineKeyboardButton(text="Две стороны", callback_data="side:1.1")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
    ])
    await state.set_state(CleanOrder.choosing_side)
    await callback.message.answer("🛏 Сколько сторон чистим?", reply_markup=kb)
    await callback.answer()


@router.callback_query(CleanOrder.choosing_side, F.data.startswith("side:"))
async def mattress_side(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(side=float(callback.data.split(":")[1]))
    await state.set_state(CleanOrder.choosing_dirt)
    await ask_dirt(callback.message)
    await callback.answer()


# ================= ПАРАМЕТРЫ ДРУГИХ ПРЕДМЕТОВ =================
@router.message(CleanOrder.entering_quantity)
async def quantity(message: types.Message, state: FSMContext):
    try:
        qty = int(message.text.strip())
        if qty <= 0 or qty > 50:
            raise ValueError
    except ValueError:
        await message.answer("❗ Введите целое число от 1 до 50")
        return
    await state.update_data(quantity=qty)
    await state.set_state(CleanOrder.choosing_dirt)
    await ask_dirt(message)


@router.message(CleanOrder.entering_area)
async def carpet_area(message: types.Message, state: FSMContext):
    try:
        area = float(message.text.strip().replace(",", "."))
        if area <= 0 or area > 10000:
            raise ValueError
    except ValueError:
        await message.answer("❗ Введите корректное число (например: 12.5)")
        return
    await state.update_data(area=area)
    await state.set_state(CleanOrder.choosing_dirt)
    await ask_dirt(message)


@router.message(CleanOrder.entering_custom)
async def custom_item(message: types.Message, state: FSMContext):
    await state.update_data(custom=message.text)
    await state.set_state(CleanOrder.choosing_dirt)
    await ask_dirt(message)


# ================= ЗАГРЯЗНЕНИЕ И ФОТО =================
async def ask_dirt(message: types.Message):
    builder = InlineKeyboardBuilder()
    for d in DIRT_MULTIPLIER:
        builder.button(text=d, callback_data=f"dirt:{d}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data="cancel"))
    await message.answer("🧼 *Степень загрязнения:*", reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(CleanOrder.choosing_dirt, F.data.startswith("dirt:"))
async def dirt(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(dirt=callback.data.split(":")[1])
    await state.set_state(CleanOrder.uploading_photos)
    await callback.message.answer(
        "📸 Пришлите фото мебели (можно несколько) или нажмите «Пропустить»",
        reply_markup=skip_photo_kb()
    )
    await callback.answer()


@router.message(CleanOrder.uploading_photos, F.photo)
async def photos(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos_list = data.get("photos", [])
    photos_list.append(message.photo[-1].file_id)
    await state.update_data(photos=photos_list)
    await message.answer(
        f"✅ Фото #{len(photos_list)} получено. Можете отправить ещё или перейти дальше.",
        reply_markup=skip_photo_kb()
    )


@router.callback_query(CleanOrder.uploading_photos, F.data == "skip_photo")
async def skip_photo(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(CleanOrder.entering_comment)
    await callback.message.answer(
        "📏 Укажите размеры или комментарий к мебели\n_Например: диван 3м или 2 стула_",
        parse_mode="Markdown",
        reply_markup=cancel_kb()
    )
    await callback.answer()


# ================= КОММЕНТАРИЙ, АДРЕС И ВРЕМЯ (ИСПРАВЛЕНО!) =================
@router.message(CleanOrder.entering_comment)
async def comment(message: types.Message, state: FSMContext):
    await state.update_data(comment=message.text)
    data = await state.get_data()

    # Проверяем, есть ли уже адрес и время от прошлой позиции
    if not data.get("address"):
        await state.set_state(CleanOrder.entering_address)
        await message.answer("📍 Введите адрес выезда мастера", reply_markup=cancel_kb())
    elif not data.get("datetime"):
        await state.set_state(CleanOrder.choosing_datetime)
        await ask_datetime(message)
    else:
        # Если всё есть, переходим к проверке телефона / финалу
        await proceed_to_phone(message, state, message.from_user.id)


@router.message(CleanOrder.entering_address)
async def address(message: types.Message, state: FSMContext):
    await state.update_data(address=message.text)
    data = await state.get_data()

    if data.get("datetime"):
        await proceed_to_phone(message, state, message.from_user.id)
    else:
        await state.set_state(CleanOrder.choosing_datetime)
        await ask_datetime(message)


async def ask_datetime(message: types.Message):
    builder = InlineKeyboardBuilder()
    tz = pytz.timezone('Europe/Moscow')
    current_hour = datetime.now(tz).hour

    slots = []
    if current_hour < 11: slots.append("Сегодня утром")
    if current_hour < 14: slots.append("Сегодня днём")
    if current_hour < 18: slots.append("Сегодня вечером")
    slots.extend(["Завтра утром", "Завтра днём", "Завтра вечером", "Другое время"])

    for slot in slots:
        builder.button(text=slot, callback_data=f"dt:{slot}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data="cancel"))

    await message.answer("🗓 *Выберите удобное время для выезда мастера:*", reply_markup=builder.as_markup(),
                         parse_mode="Markdown")


@router.callback_query(CleanOrder.choosing_datetime, F.data.startswith("dt:"))
async def choose_datetime(callback: types.CallbackQuery, state: FSMContext):
    slot = callback.data.split(":", 1)[1]
    if slot == "Другое время":
        await callback.message.answer(
            "📅 Введите удобное вам дату и время\n_Например: пятница после 15:00_",
            parse_mode="Markdown", reply_markup=cancel_kb()
        )
        await state.update_data(awaiting_custom_dt=True)
    else:
        await state.update_data(datetime=slot, awaiting_custom_dt=False)
        await proceed_to_phone(callback.message, state, callback.from_user.id)
    await callback.answer()


@router.message(CleanOrder.choosing_datetime)
async def custom_datetime(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get("awaiting_custom_dt"):
        await state.update_data(datetime=message.text.strip(), awaiting_custom_dt=False)
        await proceed_to_phone(message, state, message.from_user.id)


# ================= ПОДТВЕРЖДЕНИЕ ТЕЛЕФОНА =================
async def proceed_to_phone(message: types.Message, state: FSMContext, user_id: int):
    data = await state.get_data()

    if data.get("phone"):
        await finish_order(message, state)
        return

    saved_phone = users_db.get(user_id, {}).get("phone", "")
    await state.set_state(CleanOrder.confirming_phone)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Телефон верный: {saved_phone}", callback_data="phone_ok")],
        [InlineKeyboardButton(text="✏️ Изменить номер", callback_data="phone_change")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
    ])
    await message.answer("📞 *Проверьте контактный телефон:*", reply_markup=kb, parse_mode="Markdown")


@router.callback_query(CleanOrder.confirming_phone, F.data == "phone_ok")
async def phone_ok(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    phone = users_db.get(user_id, {}).get("phone", "")
    await state.update_data(phone=phone)
    await finish_order(callback.message, state)
    await callback.answer()


@router.callback_query(CleanOrder.confirming_phone, F.data == "phone_change")
async def phone_change(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(CleanOrder.entering_new_phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await callback.message.answer("📞 Введите новый номер или поделитесь контактом:", reply_markup=kb)
    await callback.answer()


@router.message(CleanOrder.entering_new_phone)
async def new_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text.strip()
    digits = phone.replace("+", "").replace(" ", "").replace("-", "")
    if not digits.isdigit() or len(digits) < 10:
        await message.answer("❗ Введите корректный номер телефона")
        return

    user_id = message.from_user.id
    if user_id in users_db:
        users_db[user_id]["phone"] = phone
    await state.update_data(phone=phone)
    await message.answer("✅ Номер обновлён!", reply_markup=ReplyKeyboardRemove())
    await finish_order(message, state)


# ================= РАСЧЁТ ЦЕНЫ =================
def calculate_item_price(data: dict) -> int:
    item = data.get("item")
    dirt = data.get("dirt", "Лёгкая")
    multiplier = DIRT_MULTIPLIER.get(dirt, 1.0)

    if item == "Диван":
        sofa_type = data.get("sofa_type", "Прямой")
        base = SOFA_PRICES.get(sofa_type, 3000)
        return int(base * multiplier)
    elif item == "Матрас":
        mattress_size = data.get("mattress_size", "160 см")
        base = MATTRESS_PRICES.get(mattress_size, 3500)
        side = data.get("side", 1.0)
        return int(base * side * multiplier)
    elif item == "Ковёр":
        area = data.get("area", 0)
        price_m = 250 if area >= 100 else 300 if area >= 60 else 350 if area >= 30 else 400
        return int(area * price_m * multiplier)
    else:
        base = BASE_PRICES.get(item, 1200)
        return int(base * data.get("quantity", 1) * multiplier)


# ================= ФОРМИРОВАНИЕ ИТОГОВОГО ЗАКАЗА (ИСПРАВЛЕНО!) =================
def build_order_summary(data: dict, user_id: int) -> tuple[str, int, bool, bool]:
    items_list = data.get("items_list", [])
    current = _extract_current_item(data)
    all_items = items_list + ([current] if current else [])

    lines = []
    total = 0
    positions_count = 0

    # Флаги для объединения стульев и кресел в "одну позицию" для скидки
    has_chair = False
    has_armchair = False

    for i, it in enumerate(all_items, 1):
        name = it["item"]
        if name == "Диван" and it.get("sofa_type"):
            name += f" ({it['sofa_type']})"
        elif name == "Матрас" and it.get("mattress_size"):
            sides = "2 стороны" if it.get("side", 1.0) > 1 else "1 сторона"
            name += f" {it['mattress_size']}, {sides}"
        elif name == "Ковёр":
            name += f" {it.get('area', '')} м²"
        elif name in ["Кресло", "Стул"] and it.get("quantity", 1) > 1:
            name += f" ×{it['quantity']}"
        elif name == "Другое" and it.get("custom"):
            name += f" ({it['custom']})"

        price = it.get("price") or calculate_item_price(it)
        lines.append(f"  {i}. {name} — {price} ₽")
        total += price

        # Умный подсчет "позиций" для скидки 15%
        if it["item"] == "Стул":
            if not has_chair:
                positions_count += 1
                has_chair = True
        elif it["item"] == "Кресло":
            if not has_armchair:
                positions_count += 1
                has_armchair = True
        else:
            # Каждый Диван, Ковер, Матрас и т.д. добавляет +1 к числу позиций
            positions_count += 1

    volume_discount = positions_count >= 3
    new_client_discount = not user_has_orders(user_id)

    if volume_discount:
        total = int(total * 0.85)
    elif new_client_discount:
        total = int(total * 0.90)

    return "\n".join(lines), total, new_client_discount, volume_discount


# ================= ФИНАЛ =================
async def finish_order(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.chat.id

    items_text, total, is_new_client, is_volume = build_order_summary(data, user_id)

    dt = data.get("datetime", "не указано")
    phone = data.get("phone", "—")

    discount_line = ""
    if is_volume:
        discount_line = "\n🏷 *Скидка 15%* за 3+ позиции применена!"
    elif is_new_client:
        discount_line = "\n🎉 *Скидка 10%* для нового клиента применена!"

    await message.answer(
        f"✅ *Заявка сформирована!*\n\n"
        f"📋 *Состав заказа:*\n{items_text}\n"
        f"{discount_line}\n"
        f"💰 Итого: ~{total} ₽\n\n"
        f"🗓 {dt}\n"
        f"📞 {phone}\n\n"
        "Нажмите *Подтвердить* или добавьте ещё позицию:",
        reply_markup=confirm_order_kb(),
        parse_mode="Markdown"
    )

    await state.update_data(final_price=total, is_new_client=is_new_client, is_volume=is_volume)


# ================= ПОДТВЕРЖДЕНИЕ =================
@router.callback_query(F.data == "confirm")
async def confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = callback.from_user
    user_info = users_db.get(user.id, {})

    name = user_info.get("name", user.full_name)
    client_link = f"@{user.username}" if user.username else f"<a href='tg://user?id={user.id}'>{name}</a>"

    items_list = data.get("items_list", [])
    current = _extract_current_item(data)
    all_items = items_list + ([current] if current else [])

    final_price = data.get("final_price", 0)
    is_new_client = data.get("is_new_client", False)
    is_volume = data.get("is_volume", False)

    items_text_admin = ""
    for i, it in enumerate(all_items, 1):
        iname = it["item"]
        if iname == "Диван" and it.get("sofa_type"):
            iname += f" ({it['sofa_type']})"
        elif iname == "Матрас" and it.get("mattress_size"):
            sides = "2 стороны" if it.get("side", 1.0) > 1 else "1 сторона"
            iname += f" {it['mattress_size']}, {sides}"
        elif iname == "Ковёр":
            iname += f" {it.get('area', '')} м²"
        elif iname in ["Кресло", "Стул"] and it.get("quantity", 1) > 1:
            iname += f" ×{it['quantity']}"

        price_i = it.get("price") or calculate_item_price(it)
        dirt_i = it.get("dirt", "—")
        comment_i = it.get("comment", "—")
        items_text_admin += f"  {i}. {iname} | {dirt_i} | {price_i} ₽ | комм: {comment_i}\n"

    discount_note = ""
    if is_volume:
        discount_note = " (скидка -15% за объём)"
    elif is_new_client:
        discount_note = " (скидка -10% новый клиент)"

    caption = (
        f"🔥 <b>НОВАЯ ЗАЯВКА</b>\n\n"
        f"👤 {client_link} ({name})\n"
        f"📋 Позиции:\n{items_text_admin}"
        f"📍 {data.get('address', '—')}\n"
        f"🗓 {data.get('datetime', '—')}\n"
        f"📞 {data.get('phone', '—')}\n"
        f"💰 Итого: ~{final_price} ₽{discount_note}"
    )

    all_photos = []
    for it in all_items:
        all_photos.extend(it.get("photos", []))
    media = [InputMediaPhoto(media=p) for p in all_photos]

    if orders_sheet:
        now_txt = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
        items_str = "; ".join([
            f"{it['item']}" + (f" ({it.get('sofa_type', '')})" if it.get('sofa_type') else "")
            for it in all_items
        ])
        try:
            orders_sheet.append_row([
                now_txt,
                str(user.id),
                name,
                data.get("phone", "—"),
                user.username or "",
                items_str,
                final_price,
                data.get("address", "—"),
                data.get("datetime", "—"),
                discount_note.strip("() ") if discount_note else "—"
            ])
        except Exception as e:
            logger.error(f"Ошибка записи заказа в таблицу: {e}")

    try:
        if media:
            media[0].caption = caption
            media[0].parse_mode = "HTML"
            await bot.send_media_group(ADMIN_ID, media)
        else:
            await bot.send_message(ADMIN_ID, caption, parse_mode="HTML")
        logger.info(f"Заявка от {user.id} ({name}): {len(all_items)} поз., {final_price} ₽")
    except Exception as e:
        logger.error(f"Ошибка отправки заявки: {e}")
        await callback.message.answer("⚠️ Ошибка при отправке. Попробуйте позже.")
        return

    await callback.message.edit_text(
        f"✅ *{name}, ваша заявка принята!*\n"
        "Мастер свяжется с вами в ближайшее время 🌿\n\n"
        "Хотите оформить ещё одну чистку?",
        reply_markup=order_again_kb(),
        parse_mode="Markdown"
    )

    await state.clear()
    await callback.answer()


# ================= ЗАПУСК =================
async def main():
    sync_users_from_table()
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())