# Schedule Parser с системой отзывов

API для работы с расписанием финансового университета и системой отзывов на преподавателей.

## Возможности

### Расписание
- Получение расписания групп и преподавателей
- Поиск групп и преподавателей
- Фильтрация по дисциплинам, аудиториям, преподавателям
- Кэширование данных в Redis

### Система отзывов
- Рейтинги преподавателей
- Комментарии с тегами-особенностями
- Загрузка изображений преподавателей и лекций
- Хранение данных в PostgreSQL

## Запуск

### 1. Настройка окружения

Скопируйте `.env.example` в `.env` и настройте:

```bash
cp .env.example .env
```

Отредактируйте пароли и другие настройки в `.env` файле.

### 2. Запуск через Docker

```bash
# Сборка и запуск всех сервисов
docker-compose up -d --build

# Просмотр логов
docker-compose logs -f

# Остановка
docker-compose down
```

### 3. Проверка работы

API будет доступно по адресу: `http://localhost:3434`

Проверьте статус кэша: `http://localhost:3434/api/cacheStats`

## API Endpoints

### Расписание

- `GET/POST /api/getRUZ` - получение расписания
- `GET/POST /api/getFilterOptions` - опции для фильтров
- `GET/POST /api/search` - поиск групп/преподавателей

### Отзывы и рейтинги

- `GET/POST /api/getEblanRating` - рейтинг преподавателя
- `POST /api/createEblanComment` - создание отзыва
- `GET/POST /api/getCommentsByEblan` - получение отзывов
- `POST /api/uploadEblanImg` - загрузка фото преподавателя  
- `POST /api/uploadLectImg` - загрузка фото с лекции
- `GET/POST /api/getLectImgs` - получение фото лекций

### Служебные

- `POST /api/clearCache` - очистка всех кэшей
- `POST /api/clearGroupCache` - очистка кэша группы
- `GET /api/cacheStats` - статистика кэша

## Структура проекта

```
.
├── app.py                 # Основное приложение
├── docker-compose.yml     # Docker конфигурация
├── Dockerfile            # Docker образ приложения
├── requirements.txt      # Python зависимости
├── init.sql             # SQL скрипт инициализации БД
├── .env                 # Переменные окружения (создать!)
├── uploads/             # Папка для загруженных файлов
│   ├── eblans/         # Фото преподавателей
│   └── lectures/       # Фото с лекций
└── README.md           # Этот файл
```

## База данных

Используется PostgreSQL со следующими таблицами:

- `eblans` - информация о преподавателях
- `eblan_comments` - отзывы и рейтинги  
- `lecture_images` - изображения с лекций

## Кэширование

Используется Redis для кэширования:
- Данных расписания (TTL: 30 мин)
- Результатов поиска (TTL: 10 мин)
- Опций фильтров (TTL: 30 мин)

## Загрузка файлов

- Поддерживаемые форматы: PNG, JPG, JPEG, GIF, WebP
- Максимальный размер: 16MB
- Автоматическое сжатие и изменение размера
- Уникальные имена файлов

## Разработка

### Локальный запуск без Docker

1. Установите зависимости:
```bash
pip install -r requirements.txt
```

2. Настройте PostgreSQL и Redis

3. Создайте `.env` файл с настройками

4. Запустите приложение:
```bash
python app.py
```

### Очистка данных

```bash
# Остановить и удалить все данные
docker-compose down -v

# Перезапустить с чистой БД
docker-compose up -d --build
```

## Мониторинг

- Логи приложения: `docker-compose logs schedparse`
- Логи базы данных: `docker-compose logs postgres`  
- Логи Redis: `docker-compose logs redis`
- Статистика кэша: `GET /api/cacheStats`
- Healthcheck: встроены в docker-compose.yml