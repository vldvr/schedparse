# Инструкция по настройке системы отзывов

## Что добавлено к существующему API

### Новые зависимости
- **PostgreSQL** - основная база данных для отзывов
- **Pillow** - обработка изображений
- **psycopg2-binary** - драйвер PostgreSQL

### Новые эндпоинты для отзывов
1. `/api/getEblanRating` - получение рейтинга преподавателя
2. `/api/createEblanComment` - создание отзыва
3. `/api/getCommentsByEblan` - получение всех отзывов
4. `/api/uploadEblanImg` - загрузка фото преподавателя
5. `/api/uploadLectImg` - загрузка фото с лекции
6. `/api/getLectImgs` - получение фото лекций

### Новые таблицы БД
- `eblans` - информация о преподавателях
- `eblan_comments` - отзывы и рейтинги
- `lecture_images` - фотографии с лекций

## Пошаговая настройка

### 1. Файлы для создания
Создайте следующие файлы в корне проекта:

```bash
# Скопируйте конфигурацию окружения
cp .env.example .env

# Создайте папки для загрузок
mkdir -p uploads/eblans
mkdir -p uploads/lectures

# Сделайте скрипт деплоя исполняемым
chmod +x deploy.sh
```

### 2. Настройка .env файла
Отредактируйте `.env` файл:

```bash
# ОБЯЗАТЕЛЬНО смените пароль!
DB_PASSWORD=ваш_безопасный_пароль_здесь

# Опционально - смените другие настройки
DB_USER=schedparse_user
DB_NAME=schedparse_db
```

### 3. Замена файлов
Замените существующие файлы:
- `app.py` - основное приложение с новыми эндпоинтами
- `requirements.txt` - добавлены новые зависимости
- `docker-compose.yml` - добавлен PostgreSQL
- `Dockerfile` - добавлены системные зависимости
- `.gitignore` - добавлены папки uploads

### 4. Запуск
```bash
# Автоматический деплой
./deploy.sh

# Или ручками:
docker-compose up -d --build
```

### 5. Проверка работы
```bash
# Проверка API
curl http://localhost:3434/api/cacheStats

# Проверка базы данных
docker-compose exec postgres psql -U schedparse_user -d schedparse_db -c "\\dt"

# Просмотр логов
docker-compose logs -f
```

## Тестирование новых эндпоинтов

### Получение рейтинга преподавателя
```bash
curl -X POST http://localhost:3434/api/getEblanRating \
  -H "Content-Type: application/json" \
  -d '{"eblanId": 12345, "lectString": "В4/1488"}'
```

### Создание отзыва
```bash
curl -X POST http://localhost:3434/api/createEblanComment \
  -H "Content-Type: application/json" \
  -d '{
    "eblanId": 12345,
    "rating": 4,
    "comment": "Хороший преподаватель",
    "features": ["Объясняет понятно", "Не опаздывает"]
  }'
```

### Загрузка фото преподавателя
```bash
curl -X POST http://localhost:3434/api/uploadEblanImg \
  -F "eblanId=12345" \
  -F "file=@photo.jpg"
```

### Получение отзывов
```bash
curl -X POST http://localhost:3434/api/getCommentsByEblan \
  -H "Content-Type: application/json" \
  -d '{"eblanId": 12345}'
```

## Возможные проблемы и решения

### База данных не подключается
```bash
# Проверьте логи PostgreSQL
docker-compose logs postgres

# Проверьте переменные окружения
docker-compose exec schedparse env | grep DB_
```

### Загрузка файлов не работает
```bash
# Проверьте права на папки
ls -la uploads/
sudo chown -R 1000:1000 uploads/

# Проверьте размер файла (макс 16MB)
```

### Redis недоступен
```bash
# Перезапустите Redis
docker-compose restart redis

# Проверьте статус
docker-compose exec redis redis-cli ping
```

## Мониторинг и обслуживание

### Просмотр статистики
- Кэш: `GET /api/cacheStats`
- Логи: `docker-compose logs -f`
- Состояние: `docker-compose ps`

### Резервное копирование БД
```bash
# Создать бэкап
docker-compose exec postgres pg_dump -U schedparse_user schedparse_db > backup.sql

# Восстановить из бэкапа
docker-compose exec -T postgres psql -U schedparse_user -d schedparse_db < backup.sql
```

### Очистка данных
```bash
# Остановить и удалить все данные
docker-compose down -v

# Перезапуск с чистой БД
docker-compose up -d --build
```

## Интеграция с фронтендом

Все эндпоинты поддерживают CORS и возвращают JSON.

Обратите внимание:
- Поля `eblanId` всегда числовые
- Рейтинг от 1 до 5
- Загружаемые файлы автоматически сжимаются
- Изображения доступны по пути `/images/eblans/` и `/images/lectures/`

Удачи с проектом! 🚀