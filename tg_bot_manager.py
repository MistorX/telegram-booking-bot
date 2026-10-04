from aiogram import types
from dotenv import load_dotenv
import aiogram
import asyncio
import os
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram import F
import time
import re
import aiosqlite
from datetime import datetime, timedelta
from aiogram.utils.keyboard import InlineKeyboardBuilder
import logging


load_dotenv()
bot = aiogram.Bot(token=os.getenv('TOKEN'))
dp = aiogram.Dispatcher()
id_admin = os.getenv('ADMIN_ID')
WEEKDAY_NAMES = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
DB = 'ww.db'

async def init_db():
    """Создаёт таблицы, если их ещё нет. Вызывается один раз при старте бота."""
    async with aiosqlite.connect(DB) as db:
        # сюда падают подтверждённые записи пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS zapis(
                name TEXT,
                email TEXT,
                date TEXT,   -- формат 'YYYY-MM-DD'
                time TEXT,    -- формат 'HH:MM'
                chat_id TEXT,
                reminded INTEGER DEFAULT 0
            )
        """)
        # сюда падает расписание админа: одна строка = один рабочий день недели
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedule(
                weekday INTEGER PRIMARY KEY,  -- 0=Пн ... 6=Вс
                start_time TEXT,              -- 'HH:MM', с какого часа принимаем
                end_time TEXT,                -- 'HH:MM', до какого часа принимаем
                interval_min INTEGER          -- шаг между слотами в минутах
            )
        """)
        await db.commit()


#данные для анкеты
class Form(StatesGroup):
    choosing_day = State()
    choosing_time = State()
    name = State()
    email = State()

class ScheduleForm(StatesGroup):
    choosing_days = State()
    start_time = State()
    end_time = State()
    interval = State()

async def get_all_records():
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT name, email, date, time FROM zapis ORDER BY date, time"
        ) as cursor:
            return await cursor.fetchall()

#клавиатуры 

def get_murkup():
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text='новая запись')]],
        resize_keyboard=True
    )

def get_murkup_schedule():
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text='список записей')]],
        resize_keyboard=True
    )

#клавиатура с рабочими днями для пользователя 
async def available_days_keyboard():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT weekday FROM schedule") as cur:
            rows = await cur.fetchall()
    working_weekdays = {r[0] for r in rows}
    if not working_weekdays:
        return None

    builder = InlineKeyboardBuilder()
    today = datetime.now().date()
    count = 0
    offset = 0
    while count < 14 and offset < 60:
        d = today + timedelta(days=offset)
        if d.weekday() in working_weekdays:
            label = f"{WEEKDAY_NAMES[d.weekday()]} {d.strftime('%d.%m')}"
            builder.button(text=label, callback_data=f"day:{d.isoformat()}")
            count += 1
        offset += 1
    builder.adjust(3)
    return builder.as_markup()

#клавиатура со свободным временем для пользователя 
async def available_times_keyboard(date_str: str):
    d = datetime.strptime(date_str, '%Y-%m-%d').date()
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT start_time, end_time, interval_min FROM schedule WHERE weekday = ?",
            (d.weekday(),)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        start_time, end_time, interval_min = row

        async with db.execute(
            "SELECT time FROM zapis WHERE date = ?", (date_str,)
        ) as cur:
            taken = {r[0] for r in await cur.fetchall()}

    start_dt = datetime.combine(d, datetime.strptime(start_time, '%H:%M').time())
    end_dt = datetime.combine(d, datetime.strptime(end_time, '%H:%M').time())
    now = datetime.now()

    builder = InlineKeyboardBuilder()
    slot = start_dt
    count = 0
    while slot < end_dt:
        slot_str = slot.strftime('%H:%M')
        if slot_str not in taken and slot > now:
            builder.button(text=slot_str, callback_data=f"time:{date_str}:{slot_str}")
            count += 1
        slot += timedelta(minutes=interval_min)

    if count == 0:
        return None
    builder.adjust(4)
    return builder.as_markup()

#клавиатура выбора рабочих дней для админа
def weekdays_keyboard(selected: set):
    builder = InlineKeyboardBuilder()
    for i, name in enumerate(WEEKDAY_NAMES):
        mark = '✅ ' if i in selected else ''
        builder.button(text=f"{mark}{name}", callback_data=f"wd:{i}")
    builder.button(text='Готово ▶️', callback_data='wd_done')
    builder.adjust(4, 4)
    return builder.as_markup()

#проверяет строку вида 'H:MM' / 'HH:MM' и приводит к 'HH:MM'
def parse_hhmm(text: str):
    if not re.fullmatch(r'\d{1,2}:\d{2}', text):
        return None
    h, m = map(int, text.split(':'))
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return f"{h:02d}:{m:02d}"


    
#обработчик команды /start  
@dp.message(Command('start'))
@dp.message(F.text == 'новая запись')
@dp.message(F.text == 'список записей')
async def hello(message: types.Message, state: FSMContext):
    if str(message.from_user.id) == id_admin:
        rows = await get_all_records()
        if not rows:
            await message.answer('записей пока нет', reply_markup=get_murkup_schedule())
            return
        await message.answer('вот список записей:', reply_markup=get_murkup_schedule())
        for name, email, date, time_ in rows:
            await message.answer(
                f"Имя: {name}\nEmail: {email}\nДата: {date}\nВремя: {time_}"
            )
        return

    kb = await available_days_keyboard()
    if kb is None:
        await message.answer('свободных дней пока нет, попробуйте позже')
        return
    await message.answer('выберите день записи:', reply_markup=kb)
    await state.set_state(Form.choosing_day)

#далее идут обработчики состояний
@dp.callback_query(Form.choosing_day, F.data.startswith('day:'))
async def choose_day(callback: types.CallbackQuery, state: FSMContext):
    date_str = callback.data.split(':', 1)[1]
    kb = await available_times_keyboard(date_str)
    if kb is None:
        await callback.answer('на этот день нет свободного времени', show_alert=True)
        return
    await state.update_data(date=date_str)
    await callback.message.edit_text(f'день: {date_str}\nтеперь выберите время:', reply_markup=kb)
    await state.set_state(Form.choosing_time)
    await callback.answer()


@dp.callback_query(Form.choosing_time, F.data.startswith('time:'))
async def choose_time(callback: types.CallbackQuery, state: FSMContext):
    _, date_str, time_str = callback.data.split(':', 2)

    # слот могли занять, пока пользователь выбирал — перепроверяем перед тем как спросить имя
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT 1 FROM zapis WHERE date = ? AND time = ?", (date_str, time_str)
        ) as cur:
            if await cur.fetchone():
                await callback.answer('это время уже заняли, выберите другое', show_alert=True)
                kb = await available_times_keyboard(date_str)
                if kb:
                    await callback.message.edit_reply_markup(reply_markup=kb)
                return

    await state.update_data(date=date_str, time=time_str)
    await callback.message.edit_text(f'дата: {date_str}, время: {time_str}\nвведите своё имя')
    await state.set_state(Form.name)
    await callback.answer()


@dp.message(Form.name, F.text)
async def form_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer('введите свой email')
    await state.set_state(Form.email)


@dp.message(Form.email, F.text)
async def form_email(message: types.Message, state: FSMContext):
    if '@' not in message.text:
        await message.answer('несуществующий email, попробуйте ещё раз')
        return
    await state.update_data(email=message.text)
    data = await state.get_data()

    async with aiosqlite.connect(DB) as db:
        # финальная проверка 
        async with db.execute(
            "SELECT 1 FROM zapis WHERE date = ? AND time = ?", (data['date'], data['time'])
        ) as cur:
            if await cur.fetchone():
                await message.answer('это время уже заняли, начните запись заново: /start')
                await state.clear()
                return
        chat_id = message.chat.id
        # добавляет запись в таблицу
        await db.execute(
            "INSERT INTO zapis (name, email, date, time, chat_id) VALUES (?, ?, ?, ?, ?)",
            (data['name'], data['email'], data['date'], data['time'], chat_id)
        )
        await db.commit()

    await message.answer(
        f" {data['name']} Спасибо за запись!\nДата: {data['date']} \nВремя: {data['time']}",
        reply_markup=get_murkup()
    )

    # Отправка уведомления админу
    await bot.send_message(
        chat_id=id_admin,
        text=f"Новая запись:\nИмя: {data['name']}\nEmail: {data['email']}\nДата: {data['date']}\nВремя: {data['time']}"
    )


    await state.clear()

#настройка расписания админом /schedule
@dp.message(Command('schedule'))
async def schedule_start(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != id_admin:
        return
    await state.update_data(days=[])
    await message.answer('выберите рабочие дни:', reply_markup=weekdays_keyboard(set()))
    await state.set_state(ScheduleForm.choosing_days)


#выбор дней недели
@dp.callback_query(ScheduleForm.choosing_days, F.data.startswith('wd:'))
async def schedule_toggle_day(callback: types.CallbackQuery, state: FSMContext):
    day = int(callback.data.split(':')[1])
    data = await state.get_data()
    days = set(data.get('days', []))
    days.symmetric_difference_update({day})
    await state.update_data(days=list(days))
    await callback.message.edit_reply_markup(reply_markup=weekdays_keyboard(days))
    await callback.answer()


#выбор начала времени работы
@dp.callback_query(ScheduleForm.choosing_days, F.data == 'wd_done')
async def schedule_days_done(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('days'):
        await callback.answer('выберите хотя бы один день', show_alert=True)
        return
    await callback.message.edit_text('введите время начала приёма (например 10:00)')
    await state.set_state(ScheduleForm.start_time)
    await callback.answer()


#выбор конца времени работы
@dp.message(ScheduleForm.start_time, F.text)
async def schedule_start_time(message: types.Message, state: FSMContext):
    t = parse_hhmm(message.text)
    if not t:
        await message.answer('неверный формат, введите время как "10:00"')
        return
    await state.update_data(start_time=t)
    await message.answer('введите время окончания приёма (например 18:00)')
    await state.set_state(ScheduleForm.end_time)


@dp.message(ScheduleForm.end_time, F.text)
async def schedule_end_time(message: types.Message, state: FSMContext):
    t = parse_hhmm(message.text)
    data = await state.get_data()
    if not t or t <= data['start_time']:
        await message.answer('время окончания должно быть позже начала, попробуйте ещё раз')
        return
    await state.update_data(end_time=t)
    await message.answer('введите интервал между записями в минутах (например 30)')
    await state.set_state(ScheduleForm.interval)


#выбор интервала между приёмами
@dp.message(ScheduleForm.interval, F.text)
async def schedule_interval(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer('введите положительное число минут')
        return
    interval = int(message.text)
    data = await state.get_data()

    async with aiosqlite.connect(DB) as db:
        placeholders = ','.join('?' * len(data['days']))
        await db.execute(f"DELETE FROM schedule WHERE weekday IN ({placeholders})", data['days'])
        await db.executemany(
            "INSERT INTO schedule (weekday, start_time, end_time, interval_min) VALUES (?, ?, ?, ?)",
            [(d, data['start_time'], data['end_time'], interval) for d in data['days']]
        )
        await db.commit()

    days_str = ', '.join(WEEKDAY_NAMES[d] for d in sorted(data['days']))
    await message.answer(
        f"расписание сохранено:\nдни: {days_str}\nс {data['start_time']} до {data['end_time']}\n"
        f"интервал: {interval} мин"
    )
    await state.clear()


async def main():
    await init_db()
    cleaner = asyncio.create_task(delete_old_records())
    try:
        await dp.start_polling(bot)
    finally:
        cleaner.cancel()

#удаляет запись через 30 минут после её начала и присылает напоминания за час
async def delete_old_records():
    while True:
        try:
            now = datetime.now()
            async with aiosqlite.connect(DB) as db:
                async with db.execute(
                    "SELECT rowid, date, time, chat_id FROM zapis WHERE reminded = 0"
                ) as cur:
                    rows = await cur.fetchall()
                for rowid, date_, time_, chat_id in rows:
                    start = datetime.strptime(f"{date_} {time_}", "%Y-%m-%d %H:%M")
                    left = int((start - now).total_seconds() // 60)
                    if 0 <= left <= 60:
                        await bot.send_message(chat_id, f"Напоминание: запись через {left} мин")
                        await db.execute("UPDATE zapis SET reminded = 1 WHERE rowid = ?", (rowid,))
                await db.execute(
                    "DELETE FROM zapis WHERE datetime(date || ' ' || time) "
                    "< datetime('now', 'localtime', '-30 minutes')"
                )
                await db.commit()
        except Exception:
            logging.exception("reminder loop failed")
        await asyncio.sleep(60)


if __name__ == '__main__':
    asyncio.run(main())
