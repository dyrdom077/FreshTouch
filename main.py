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
        # Убедитесь, что файл service_account.json лежит в папке с ботом
        creds = ServiceAccountCredentials.from_json_keyfile_name("FreshTouch-service_account.json", scope)
        client = gspread.authorize(creds)
        # Открываем таблицу "FreshTouch_Orders"
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
    if not users_sheet: return
    try:
        records = users_sheet.get_all_records()
        for rec in records:
            users_db[int(rec['user_id'])] = {
                "name": str(rec['name']),
                "phone": str(rec['phone']),
                "username": str(rec.get('username', ''))
            }
        logger.info(f"Синхронизировано пользователей из таблицы: {len(users_db)}")
    except Exception as e:
        logger.error(f"Ошибка синхронизации БД: {e}")


# ================= FSM =================
class Registration(StatesGroup):
    entering_name = State()
    entering_phone = State()


class CleanOrder(StatesGroup):
    choosing_item = State()
    entering_quantity = State()
    choosing_side = State()
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
BASE_PRICES = {
    "Диван": 3500,
    "Кресло": 1500,
    "Стул": 550,
    "Матрас": 4000,
    "Другое": 1200,
}

DIRT_MULTIPLIER = {
    "Лёгкая": 1.0,
    "Средняя": 1.2,
    "Сильная": 1.5,
}


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

    # Уже зарегистрирован — сразу в меню
    if user_id in users_db:
        name = users_db[user_id]["name"]
        await message.answer(
            f"👋 С возвращением, *{name}*!\n"
            "Чем могу помочь?",
            reply_markup=main_menu_kb(),
            parse_mode="Markdown"
        )
        return

    # Новый пользователь — регистрация
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

    # Обновляем локальную базу
    users_db[user_id] = {
        "name": name,
        "phone": phone,
        "username": username,
    }

    # Записываем в Google Таблицу (Лист "Пользователи")
    if users_sheet:
        try:
            users_sheet.append_row([user_id, name, phone, username])
        except Exception as e:
            logger.error(f"Ошибка записи пользователя в таблицу: {e}")

    logger.info(f"Зарегистрирован пользователь {user_id}: {name}, {phone}")

    await state.clear()

    # 1. Убираем Reply-кнопку (номер телефона)
    await message.answer(
        f"✅ Отлично, *{name}*! Вы зарегистрированы.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )

    # 2. Отправляем меню один раз
    await message.answer(
        "Чем могу помочь? Выберите действие 👇",
        reply_markup=main_menu_kb()
    )


# ================= АКЦИИ =================
@router.callback_query(F.data == "promotions")
async def promotions(callback: types.CallbackQuery):
    await callback.message.answer(
        "📚 *Мини-гайд по уходу за мебелью*\n\n"
        "1️⃣ Не трите пятна горячей водой\n"
        "2️⃣ Используйте pH-нейтральные средства\n"
        "3️⃣ Промакивайте пятна, а не трите\n\n"
        "🔥 *Скидка 10% на первый заказ активирована!*\n\n"
        "Готовы заказать чистку? 👇",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🧼 Заказать чистку", callback_data="start_calc")],
                [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="main_menu")],
            ]
        ),
        parse_mode="Markdown",
    )
    await callback.answer()


# ================= ГЛАВНОЕ МЕНЮ (callback) =================
@router.callback_query(F.data == "main_menu")
async def main_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    name = users_db.get(user_id, {}).get("name", "")
    greeting = f"*{name}*, чем могу помочь?" if name else "Чем могу помочь?"
    await callback.message.answer(greeting, reply_markup=main_menu_kb(), parse_mode="Markdown")
    await callback.answer()


# ================= ШАГ 1: ВЫБОР ПРЕДМЕТА =================
@router.callback_query(F.data == "start_calc")
async def choose_item(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    builder = InlineKeyboardBuilder()
    for item in ["Диван", "Кресло", "Стул", "Матрас", "Ковёр", "Другое"]:
        builder.button(text=item, callback_data=f"item:{item}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data="cancel"))

    await state.set_state(CleanOrder.choosing_item)
    await callback.message.answer(
        "🛋 *Что будем чистить?*",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    await callback.answer()


# ================= ВЫБОР ПРЕДМЕТА =================
@router.callback_query(CleanOrder.choosing_item, F.data.startswith("item:"))
async def item_logic(callback: types.CallbackQuery, state: FSMContext):
    item = callback.data.split(":")[1]
    await state.update_data(item=item)

    if item in ["Кресло", "Стул"]:
        await state.set_state(CleanOrder.entering_quantity)
        await callback.message.answer("🔢 Укажите количество (например: 2)", reply_markup=cancel_kb())
    elif item == "Матрас":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Одна сторона", callback_data="side:1")],
            [InlineKeyboardButton(text="Две стороны", callback_data="side:1.5")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
        ])
        await state.set_state(CleanOrder.choosing_side)
        await callback.message.answer("🛏 Сколько сторон чистим?", reply_markup=kb)
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


# ================= КОЛИЧЕСТВО =================
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


# ================= СТОРОНЫ МАТРАСА =================
@router.callback_query(CleanOrder.choosing_side, F.data.startswith("side:"))
async def mattress_side(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(side=float(callback.data.split(":")[1]))
    await state.set_state(CleanOrder.choosing_dirt)
    await ask_dirt(callback.message)
    await callback.answer()


# ================= КОВЁР: ПЛОЩАДЬ =================
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


# ================= ДРУГОЕ =================
@router.message(CleanOrder.entering_custom)
async def custom_item(message: types.Message, state: FSMContext):
    await state.update_data(custom=message.text)
    await state.set_state(CleanOrder.choosing_dirt)
    await ask_dirt(message)


# ================= ЗАГРЯЗНЕНИЕ =================
async def ask_dirt(message: types.Message):
    builder = InlineKeyboardBuilder()
    for d in DIRT_MULTIPLIER:
        builder.button(text=d, callback_data=f"dirt:{d}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data="cancel"))
    await message.answer(
        "🧼 *Степень загрязнения:*",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


@router.callback_query(CleanOrder.choosing_dirt, F.data.startswith("dirt:"))
async def dirt(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(dirt=callback.data.split(":")[1])
    await state.set_state(CleanOrder.uploading_photos)
    await callback.message.answer(
        "📸 Пришлите фото мебели (можно несколько) или нажмите «Пропустить»",
        reply_markup=skip_photo_kb()
    )
    await callback.answer()


# ================= ФОТО =================
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


# ================= КОММЕНТАРИЙ =================
@router.message(CleanOrder.entering_comment)
async def comment(message: types.Message, state: FSMContext):
    await state.update_data(comment=message.text)
    await state.set_state(CleanOrder.entering_address)
    await message.answer("📍 Введите адрес выезда мастера", reply_markup=cancel_kb())


# ================= АДРЕС =================
@router.message(CleanOrder.entering_address)
async def address(message: types.Message, state: FSMContext):
    await state.update_data(address=message.text)
    await state.set_state(CleanOrder.choosing_datetime)
    await ask_datetime(message)


# ================= ДАТА И ВРЕМЯ =================
async def ask_datetime(message: types.Message):
    builder = InlineKeyboardBuilder()

    # Устанавливаем часовой пояс Краснодара
    tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(tz)
    current_hour = now.hour

    slots = []

    # --- Логика для СЕГОДНЯ ---
    if current_hour < 14:
        slots.append("Сегодня днём")
    if current_hour < 18:
        slots.append("Сегодня вечером")

    # --- Логика для ЗАВТРА ---
    slots.extend([
        "Завтра утром",
        "Завтра днём",
        "Завтра вечером",
        "Другое время"
    ])

    for slot in slots:
        builder.button(text=slot, callback_data=f"dt:{slot}")

    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data="cancel"))

    await message.answer(
        "🗓 *Выберите удобное время для выезда мастера:*",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


@router.callback_query(CleanOrder.choosing_datetime, F.data.startswith("dt:"))
async def choose_datetime(callback: types.CallbackQuery, state: FSMContext):
    slot = callback.data.split(":", 1)[1]
    if slot == "Другое время":
        await callback.message.answer(
            "📅 Введите удобное вам дату и время\n_Например: пятница после 15:00_",
            parse_mode="Markdown",
            reply_markup=cancel_kb()
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
    saved_phone = users_db.get(user_id, {}).get("phone", "")
    await state.set_state(CleanOrder.confirming_phone)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✅ Телефон верный: {saved_phone}",
                callback_data="phone_ok"
            )],
            [InlineKeyboardButton(text="✏️ Изменить номер", callback_data="phone_change")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
        ]
    )
    await message.answer(
        "📞 *Проверьте контактный телефон:*",
        reply_markup=kb,
        parse_mode="Markdown"
    )


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
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await callback.message.answer(
        "📞 Введите новый номер или поделитесь контактом:",
        reply_markup=kb
    )
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
def calculate_price(data: dict) -> int:
    item = data["item"]
    dirt = data.get("dirt", "Лёгкая")
    if item == "Ковёр":
        area = data["area"]
        price_m = 250 if area >= 100 else 300 if area >= 60 else 350 if area >= 30 else 400
        return int(area * price_m * DIRT_MULTIPLIER.get(dirt, 1.0))
    base = BASE_PRICES.get(item, 1200)
    return int(base * data.get("quantity", 1) * data.get("side", 1.0) * DIRT_MULTIPLIER.get(dirt, 1.0))


# ================= ФИНАЛ =================
async def finish_order(message: types.Message, state: FSMContext):
    data = await state.get_data()
    price = calculate_price(data)
    await state.update_data(price=price)

    item = data["item"]
    dt = data.get("datetime", "не указано")
    phone = data.get("phone", "—")

    await message.answer(
        f"✅ *Заявка сформирована!*\n\n"
        f"🛋 {item}\n"
        f"🗓 {dt}\n"
        f"📞 {phone}\n"
        f"💰 Ориентировочная стоимость ~{price} ₽\n\n"
        "Нажмите *Подтвердить*, чтобы отправить заявку мастеру",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
            ]
        ),
        parse_mode="Markdown"
    )


# ================= ПОДТВЕРЖДЕНИЕ =================
@router.callback_query(F.data == "confirm")
async def confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = callback.from_user
    user_info = users_db.get(user.id, {})

    name = user_info.get("name", user.full_name)
    client_link = f"@{user.username}" if user.username else f"<a href='tg://user?id={user.id}'>{name}</a>"

    photos = data.get("photos", [])
    media = [InputMediaPhoto(media=p) for p in photos]

    # 1. ЗАПИСЬ В GOOGLE ТАБЛИЦУ "Заказы"
    if orders_sheet:
        now_txt = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
        try:
            # Колонки: Дата, Имя, Телефон, Ник, Предмет, Цена, Адрес, Время, Комментарий
            orders_sheet.append_row([
                now_txt,
                name,
                data.get("phone", "—"),
                user.username or "",
                data['item'],
                data.get('price', 0),
                data.get("address", "—"),
                data.get("datetime", "—"),
                data.get("comment", "—")
            ])
        except Exception as e:
            logger.error(f"Ошибка записи заказа в таблицу: {e}")

    # 2. ОТПРАВКА СООБЩЕНИЯ В TELEGRAM
    caption = (
        f"🔥 <b>НОВАЯ ЗАЯВКА</b>\n\n"
        f"👤 {client_link} ({name})\n"
        f"🛋 {data['item']}\n"
        f"🧹 Загрязнение: {data.get('dirt', '—')}\n"
        f"💬 {data.get('comment', '—')}\n"
        f"📍 {data.get('address', '—')}\n"
        f"🗓 {data.get('datetime', '—')}\n"
        f"📞 {data.get('phone', '—')}\n"
        f"💰 ~{data.get('price', '?')} ₽"
    )

    try:
        if media:
            media[0].caption = caption
            media[0].parse_mode = "HTML"
            await bot.send_media_group(ADMIN_ID, media)
        else:
            await bot.send_message(ADMIN_ID, caption, parse_mode="HTML")
        logger.info(f"Заявка от {user.id} ({name}): {data['item']}, {data.get('price')} ₽")
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
    # Загружаем базу из Google Sheets при старте
    sync_users_from_table()
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
