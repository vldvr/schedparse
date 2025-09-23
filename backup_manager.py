import os
import json
import zipfile
import shutil
import hashlib
import secrets
import tempfile
from datetime import datetime
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64
import psycopg2
from contextlib import contextmanager
import subprocess
import time

class BackupManager:
    def __init__(self, db_config, upload_folder):
        self.db_config = db_config
        self.upload_folder = upload_folder
        self.backup_folder = '/app/backups'
        self.nas_config = self._load_nas_config()
        self.ensure_backup_folder()
    
    def _load_nas_config(self):
        """Загрузка конфигурации NAS из переменных окружения"""
        return {
            'enabled': os.environ.get('NAS_BACKUP_ENABLED', 'false').lower() == 'true',
            'host': os.environ.get('NAS_HOST'),
            'share': os.environ.get('NAS_SHARE'),
            'username': os.environ.get('NAS_USERNAME'),
            'password': os.environ.get('NAS_PASSWORD'),
            'path': os.environ.get('NAS_BACKUP_PATH', 'schedparse_backups'),  # Путь внутри шары
            'domain': os.environ.get('NAS_DOMAIN', ''),  # Для доменных учеток
            'mount_point': '/mnt/nas_backup'
        }
    
    def ensure_backup_folder(self):
        """Создать папку для бэкапов если не существует"""
        os.makedirs(self.backup_folder, exist_ok=True)
        
        # Создать точку монтирования для NAS
        if self.nas_config['enabled']:
            os.makedirs(self.nas_config['mount_point'], exist_ok=True)
    
    @contextmanager
    def get_db_connection(self):
        """Контекстный менеджер для подключения к БД"""
        conn = psycopg2.connect(**self.db_config)
        try:
            yield conn
        finally:
            conn.close()
    
    def generate_key_from_password(self, password: str, salt: bytes) -> bytes:
        """Генерация ключа шифрования из пароля"""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        return base64.urlsafe_b64encode(kdf.derive(password.encode()))
    
    def create_backup(self, password: str, backup_type: str = 'manual') -> str:
        """Создать зашифрованный бэкап"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"schedparse_backup_{backup_type}_{timestamp}.encrypted"
        backup_path = os.path.join(self.backup_folder, backup_filename)
        
        # Создаем временную директорию для работы
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_data_dir = os.path.join(temp_dir, 'backup_data')
            os.makedirs(backup_data_dir)
            
            # 1. Экспорт данных из БД
            self._export_database_data(backup_data_dir)
            
            # 2. Копирование файлов изображений
            self._copy_images(backup_data_dir)
            
            # 3. Создание метаданных бэкапа
            self._create_metadata(backup_data_dir, timestamp, backup_type)
            
            # 4. Создание ZIP архива
            zip_path = os.path.join(temp_dir, 'backup.zip')
            self._create_zip_archive(backup_data_dir, zip_path)
            
            # 5. Шифрование архива
            self._encrypt_backup(zip_path, backup_path, password)
        
        print(f"Бэкап создан: {backup_filename}")
        
        # 6. Попытка загрузки на NAS (только для автоматических бэкапов)
        if backup_type == 'auto' and self.nas_config['enabled']:
            try:
                self._upload_to_nas(backup_path, backup_filename)
                print(f"Бэкап успешно загружен на NAS: {backup_filename}")
            except Exception as e:
                print(f"Ошибка загрузки на NAS: {e}")
                # Не прерываем выполнение, если NAS недоступен
        
        return backup_path
    
    def _mount_nas(self) -> bool:
        """Монтирование NAS через SMB"""
        try:
            # Проверяем, не смонтирован ли уже
            mount_check = subprocess.run(
                ['mountpoint', '-q', self.nas_config['mount_point']],
                capture_output=True
            )
            
            if mount_check.returncode == 0:
                print("NAS уже смонтирован")
                return True
            
            # Формируем команду монтирования
            mount_cmd = [
                'mount', '-t', 'cifs',
                f"//{self.nas_config['host']}/{self.nas_config['share']}",
                self.nas_config['mount_point'],
                '-o'
            ]
            
            # Опции монтирования
            options = [
                f"username={self.nas_config['username']}",
                f"password={self.nas_config['password']}",
                'uid=0,gid=0',  # Для контейнера Docker
                'iocharset=utf8',
                'file_mode=0644,dir_mode=0755'
            ]
            
            # Добавляем домен если указан
            if self.nas_config['domain']:
                options.append(f"domain={self.nas_config['domain']}")
            
            mount_cmd.append(','.join(options))
            
            # Выполняем монтирование
            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                print("NAS успешно смонтирован")
                return True
            else:
                print(f"Ошибка монтирования NAS: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            print("Таймаут при монтировании NAS")
            return False
        except Exception as e:
            print(f"Исключение при монтировании NAS: {e}")
            return False
    
    def _unmount_nas(self):
        """Размонтирование NAS"""
        try:
            subprocess.run(['umount', self.nas_config['mount_point']], 
                         capture_output=True, timeout=10)
            print("NAS размонтирован")
        except:
            pass  # Игнорируем ошибки размонтирования
    
    def _upload_to_nas(self, backup_path: str, backup_filename: str):
        """Загрузка бэкапа на NAS"""
        if not self.nas_config['enabled']:
            return
        
        # Монтируем NAS
        if not self._mount_nas():
            raise Exception("Не удалось смонтировать NAS")
        
        try:
            # Создаем папку для бэкапов на NAS если не существует
            nas_backup_dir = os.path.join(self.nas_config['mount_point'], self.nas_config['path'])
            os.makedirs(nas_backup_dir, exist_ok=True)
            
            # Копируем файл на NAS
            nas_backup_path = os.path.join(nas_backup_dir, backup_filename)
            shutil.copy2(backup_path, nas_backup_path)
            
            # Проверяем, что файл скопировался корректно
            if os.path.exists(nas_backup_path):
                local_size = os.path.getsize(backup_path)
                nas_size = os.path.getsize(nas_backup_path)
                
                if local_size == nas_size:
                    print(f"Бэкап успешно загружен на NAS: {backup_filename} ({local_size} байт)")
                    
                    # Опционально: удаляем старые бэкапы с NAS
                    self._cleanup_nas_backups(nas_backup_dir)
                else:
                    raise Exception(f"Размеры файлов не совпадают: локальный {local_size}, NAS {nas_size}")
            else:
                raise Exception("Файл не найден на NAS после копирования")
                
        finally:
            # Всегда размонтируем NAS
            self._unmount_nas()
    
    def _cleanup_nas_backups(self, nas_backup_dir: str, keep_count: int = 12):
        """Удаление старых бэкапов с NAS (оставляем последние 12 месячных)"""
        try:
            # Получаем список всех бэкапов на NAS
            backup_files = []
            for filename in os.listdir(nas_backup_dir):
                if filename.startswith('schedparse_backup_auto_') and filename.endswith('.encrypted'):
                    file_path = os.path.join(nas_backup_dir, filename)
                    stat = os.stat(file_path)
                    backup_files.append({
                        'filename': filename,
                        'path': file_path,
                        'mtime': stat.st_mtime
                    })
            
            # Сортируем по времени изменения (новые первыми)
            backup_files.sort(key=lambda x: x['mtime'], reverse=True)
            
            # Удаляем старые файлы
            for backup_file in backup_files[keep_count:]:
                try:
                    os.remove(backup_file['path'])
                    print(f"Удален старый бэкап с NAS: {backup_file['filename']}")
                except Exception as e:
                    print(f"Ошибка удаления бэкапа с NAS {backup_file['filename']}: {e}")
                    
        except Exception as e:
            print(f"Ошибка очистки старых бэкапов на NAS: {e}")
    
    def _export_database_data(self, backup_dir: str):
        """Экспорт данных из всех таблиц БД"""
        tables_data = {}
        
        with self.get_db_connection() as conn:
            with conn.cursor() as cur:
                # Экспорт таблицы eblans
                cur.execute("""
                    SELECT eblan_id, eblan_fio, eblan_img, eblan_img_approved, 
                           created_at, updated_at
                    FROM eblans
                """)
                eblans = []
                for row in cur.fetchall():
                    eblans.append({
                        'eblan_id': row[0],
                        'eblan_fio': row[1],
                        'eblan_img': row[2],
                        'eblan_img_approved': row[3],
                        'created_at': row[4].isoformat() if row[4] else None,
                        'updated_at': row[5].isoformat() if row[5] else None
                    })
                tables_data['eblans'] = eblans
                
                # Экспорт таблицы eblan_comments
                cur.execute("""
                    SELECT id, eblan_id, rating, comment, features, created_at, 
                           ip_address, reply_to_id
                    FROM eblan_comments
                """)
                comments = []
                for row in cur.fetchall():
                    comments.append({
                        'id': row[0],
                        'eblan_id': row[1],
                        'rating': row[2],
                        'comment': row[3],
                        'features': row[4],
                        'created_at': row[5].isoformat() if row[5] else None,
                        'ip_address': str(row[6]) if row[6] else None,
                        'reply_to_id': row[7]
                    })
                tables_data['eblan_comments'] = comments
                
                # Экспорт таблицы lecture_images
                cur.execute("""
                    SELECT id, eblan_id, lect_string, image_path, created_at, 
                           approved, ip_address
                    FROM lecture_images
                """)
                lecture_images = []
                for row in cur.fetchall():
                    lecture_images.append({
                        'id': row[0],
                        'eblan_id': row[1],
                        'lect_string': row[2],
                        'image_path': row[3],
                        'created_at': row[4].isoformat() if row[4] else None,
                        'approved': row[5],
                        'ip_address': str(row[6]) if row[6] else None
                    })
                tables_data['lecture_images'] = lecture_images
        
        # Сохранение данных в JSON файл
        db_export_path = os.path.join(backup_dir, 'database_export.json')
        with open(db_export_path, 'w', encoding='utf-8') as f:
            json.dump(tables_data, f, ensure_ascii=False, indent=2)
    
    def _copy_images(self, backup_dir: str):
        """Копирование всех изображений"""
        images_backup_dir = os.path.join(backup_dir, 'images')
        
        if os.path.exists(self.upload_folder):
            shutil.copytree(self.upload_folder, images_backup_dir)
        else:
            os.makedirs(images_backup_dir)
    
    def _create_metadata(self, backup_dir: str, timestamp: str, backup_type: str):
        """Создание метаданных бэкапа"""
        metadata = {
            'version': '1.0',
            'created_at': timestamp,
            'backup_type': backup_type,  # 'manual', 'auto'
            'application': 'schedparse',
            'tables_included': ['eblans', 'eblan_comments', 'lecture_images'],
            'includes_images': True,
            'nas_upload': self.nas_config['enabled'] and backup_type == 'auto'
        }
        
        metadata_path = os.path.join(backup_dir, 'metadata.json')
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
    
    def _create_zip_archive(self, source_dir: str, zip_path: str):
        """Создание ZIP архива"""
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(source_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arc_name = os.path.relpath(file_path, source_dir)
                    zipf.write(file_path, arc_name)
    
    def _encrypt_backup(self, zip_path: str, output_path: str, password: str):
        """Шифрование бэкапа"""
        # Генерация соли
        salt = os.urandom(16)
        
        # Создание ключа шифрования
        key = self.generate_key_from_password(password, salt)
        fernet = Fernet(key)
        
        # Чтение и шифрование данных
        with open(zip_path, 'rb') as input_file:
            data = input_file.read()
            encrypted_data = fernet.encrypt(data)
        
        # Сохранение зашифрованного файла с солью
        with open(output_path, 'wb') as output_file:
            output_file.write(salt)  # Первые 16 байт - соль
            output_file.write(encrypted_data)
    
    def restore_backup(self, backup_path: str, password: str) -> bool:
        """Восстановление из зашифрованного бэкапа"""
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                # 1. Расшифровка бэкапа
                zip_path = os.path.join(temp_dir, 'decrypted_backup.zip')
                self._decrypt_backup(backup_path, zip_path, password)
                
                # 2. Распаковка архива
                extract_dir = os.path.join(temp_dir, 'extracted')
                with zipfile.ZipFile(zip_path, 'r') as zipf:
                    zipf.extractall(extract_dir)
                
                # 3. Проверка метаданных
                if not self._validate_backup(extract_dir):
                    return False
                
                # 4. Восстановление данных БД
                self._restore_database_data(extract_dir)
                
                # 5. Восстановление изображений
                self._restore_images(extract_dir)
            
            return True
            
        except Exception as e:
            print(f"Ошибка восстановления бэкапа: {e}")
            return False
    
    def _decrypt_backup(self, encrypted_path: str, output_path: str, password: str):
        """Расшифровка бэкапа"""
        with open(encrypted_path, 'rb') as input_file:
            # Чтение соли (первые 16 байт)
            salt = input_file.read(16)
            encrypted_data = input_file.read()
        
        # Создание ключа из пароля и соли
        key = self.generate_key_from_password(password, salt)
        fernet = Fernet(key)
        
        # Расшифровка данных
        decrypted_data = fernet.decrypt(encrypted_data)
        
        # Сохранение расшифрованных данных
        with open(output_path, 'wb') as output_file:
            output_file.write(decrypted_data)
    
    def _validate_backup(self, extract_dir: str) -> bool:
        """Проверка корректности бэкапа"""
        metadata_path = os.path.join(extract_dir, 'metadata.json')
        db_export_path = os.path.join(extract_dir, 'database_export.json')
        
        if not os.path.exists(metadata_path) or not os.path.exists(db_export_path):
            return False
        
        try:
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
                
            # Проверяем, что это бэкап нашего приложения
            return metadata.get('application') == 'schedparse'
        except:
            return False
    
    def _restore_database_data(self, extract_dir: str):
        """Восстановление данных БД"""
        db_export_path = os.path.join(extract_dir, 'database_export.json')
        
        with open(db_export_path, 'r', encoding='utf-8') as f:
            tables_data = json.load(f)
        
        with self.get_db_connection() as conn:
            with conn.cursor() as cur:
                # Очистка существующих данных (в правильном порядке из-за FK)
                cur.execute("TRUNCATE TABLE eblan_comments RESTART IDENTITY CASCADE")
                cur.execute("TRUNCATE TABLE lecture_images RESTART IDENTITY CASCADE")
                cur.execute("TRUNCATE TABLE eblans RESTART IDENTITY CASCADE")
                
                # Восстановление eblans
                for eblan in tables_data.get('eblans', []):
                    cur.execute("""
                        INSERT INTO eblans (eblan_id, eblan_fio, eblan_img, eblan_img_approved, 
                                          created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (eblan_id) DO UPDATE SET
                            eblan_fio = EXCLUDED.eblan_fio,
                            eblan_img = EXCLUDED.eblan_img,
                            eblan_img_approved = EXCLUDED.eblan_img_approved,
                            updated_at = EXCLUDED.updated_at
                    """, (
                        eblan['eblan_id'],
                        eblan['eblan_fio'],
                        eblan['eblan_img'],
                        eblan['eblan_img_approved'],
                        eblan['created_at'],
                        eblan['updated_at']
                    ))
                
                # Восстановление lecture_images
                for img in tables_data.get('lecture_images', []):
                    cur.execute("""
                        INSERT INTO lecture_images (id, eblan_id, lect_string, image_path, 
                                                  created_at, approved, ip_address)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        img['id'],
                        img['eblan_id'],
                        img['lect_string'],
                        img['image_path'],
                        img['created_at'],
                        img['approved'],
                        img['ip_address']
                    ))
                
                # Восстановление eblan_comments
                for comment in tables_data.get('eblan_comments', []):
                    cur.execute("""
                        INSERT INTO eblan_comments (id, eblan_id, rating, comment, features,
                                                  created_at, ip_address, reply_to_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        comment['id'],
                        comment['eblan_id'],
                        comment['rating'],
                        comment['comment'],
                        comment['features'],
                        comment['created_at'],
                        comment['ip_address'],
                        comment['reply_to_id']
                    ))
                
                # Сброс последовательностей
                cur.execute("SELECT setval('eblan_comments_id_seq', COALESCE((SELECT MAX(id) FROM eblan_comments), 1), true)")
                cur.execute("SELECT setval('lecture_images_id_seq', COALESCE((SELECT MAX(id) FROM lecture_images), 1), true)")
                
                conn.commit()
    
    def _restore_images(self, extract_dir: str):
        images_backup_dir = os.path.join(extract_dir, 'images')

        # 1. Убедимся, что целевая папка uploads существует
        os.makedirs(self.upload_folder, exist_ok=True)

        # 2. Очищаем содержимое целевой папки, а не удаляем ее целиком
        if os.path.exists(self.upload_folder):
            for item in os.listdir(self.upload_folder):
                item_path = os.path.join(self.upload_folder, item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)

        # 3. Копируем содержимое из бэкапа, если оно там есть
        if os.path.exists(images_backup_dir) and os.listdir(images_backup_dir):
            # Используем `dirs_exist_ok=True` (требует Python 3.8+)
            # Это позволяет копировать в уже существующую директорию.
            shutil.copytree(images_backup_dir, self.upload_folder, dirs_exist_ok=True)
        
        # 4. Эти строки теперь не обязательны, но можно оставить для гарантии
        #    на случай, если в бэкапе не было этих папок.
        os.makedirs(os.path.join(self.upload_folder, 'eblans'), exist_ok=True)
        os.makedirs(os.path.join(self.upload_folder, 'lectures'), exist_ok=True)
    
    def get_backup_list(self) -> list:
        """Получить список доступных бэкапов"""
        backups = []
        if not os.path.exists(self.backup_folder):
            return backups
        
        for filename in os.listdir(self.backup_folder):
            if filename.startswith('schedparse_backup_') and filename.endswith('.encrypted'):
                file_path = os.path.join(self.backup_folder, filename)
                stat = os.stat(file_path)
                
                # Извлекаем тип и дату из имени файла
                try:
                    # Новый формат: schedparse_backup_auto_20240101_120000.encrypted
                    parts = filename.replace('schedparse_backup_', '').replace('.encrypted', '').split('_')
                    if len(parts) >= 3:
                        backup_type = parts[0]  # auto или manual
                        timestamp_str = '_'.join(parts[1:3])  # date_time
                    else:
                        # Старый формат для обратной совместимости
                        backup_type = 'manual'
                        timestamp_str = parts[0] if parts else filename
                    
                    created_at = datetime.strptime(timestamp_str, '%Y%m%d_%H%M%S')
                except:
                    backup_type = 'unknown'
                    created_at = datetime.fromtimestamp(stat.st_ctime)
                
                backups.append({
                    'filename': filename,
                    'path': file_path,
                    'size': stat.st_size,
                    'created_at': created_at,
                    'backup_type': backup_type
                })
        
        # Сортируем по дате создания (новые первыми)
        backups.sort(key=lambda x: x['created_at'], reverse=True)
        return backups
    
    def delete_old_backups(self, keep_count: int = 5):
        """Удалить старые локальные бэкапы, оставив только последние keep_count"""
        backups = self.get_backup_list()
        
        # Разделяем бэкапы по типам
        manual_backups = [b for b in backups if b['backup_type'] == 'manual']
        auto_backups = [b for b in backups if b['backup_type'] == 'auto']
        
        # Удаляем старые ручные бэкапы (оставляем 5)
        for backup in manual_backups[keep_count:]:
            try:
                os.remove(backup['path'])
                print(f"Удален старый ручной бэкап: {backup['filename']}")
            except Exception as e:
                print(f"Ошибка удаления бэкапа {backup['filename']}: {e}")
        
        # Удаляем старые автоматические бэкапы (оставляем 3 локально, так как основные на NAS)
        for backup in auto_backups[3:]:
            try:
                os.remove(backup['path'])
                print(f"Удален старый автоматический бэкап: {backup['filename']}")
            except Exception as e:
                print(f"Ошибка удаления бэкапа {backup['filename']}: {e}")
    
    def test_nas_connection(self) -> dict:
        """Тест подключения к NAS"""
        if not self.nas_config['enabled']:
            return {'success': False, 'error': 'NAS не включен в настройках'}
        
        try:
            # Проверяем возможность монтирования
            if self._mount_nas():
                # Пробуем создать тестовый файл
                test_dir = os.path.join(self.nas_config['mount_point'], self.nas_config['path'])
                os.makedirs(test_dir, exist_ok=True)
                
                test_file = os.path.join(test_dir, 'test_connection.tmp')
                with open(test_file, 'w') as f:
                    f.write('test')
                
                # Проверяем, что файл создался и читается
                if os.path.exists(test_file):
                    os.remove(test_file)
                    self._unmount_nas()
                    return {'success': True, 'message': 'Подключение к NAS успешно'}
                else:
                    self._unmount_nas()
                    return {'success': False, 'error': 'Не удалось создать тестовый файл'}
            else:
                return {'success': False, 'error': 'Не удалось смонтировать NAS'}
                
        except Exception as e:
            self._unmount_nas()
            return {'success': False, 'error': f'Ошибка тестирования NAS: {str(e)}'}