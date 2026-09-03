# RTSP Motion Detector

Минимальный детектор движения на Python/OpenCV. По умолчанию не открывает окно и выводит событие в stdout:

```text
MOTION timestamp=1720000000.123 area=2450
```

## Установка

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## Запуск

```bash
python motion_detector.py 'rtsp://user:password@camera/stream'
```

Полезные параметры:

- `--width 640` уменьшает изображение до ширины 640 перед анализом.
- `--process-fps 5` ограничивает частоту анализа.
- `--min-area 900` задает минимальную площадь движения.
- `--display` включает отладочное окно, но заметно увеличивает расход ресурсов.

Для слабого компьютера можно начать с `--width 480 --process-fps 2`. События удобно читать другим процессом из stdout.

## Home Assistant Add-on

Добавьте каталог проекта как локальный add-on repository в Home Assistant:

1. Скопируйте проект в `/addons/rtsp-motion-detector` на хосте Home Assistant.
2. Откройте **Settings -> Add-ons -> Add-on Store**.
3. В меню нажмите **Check for updates**, затем найдите **RTSP Motion Detector** в разделе локальных add-ons.
4. Установите и откройте вкладку **Configuration**.
5. Укажите `rtsp_url` и, при необходимости, `webhook_url`.

Все параметры из `config.yaml` доступны для настройки через UI Home Assistant. `.env` не используется.

Если задан `webhook_url`, при движении отправляется POST с JSON:

```json
{"event":"motion","timestamp":1720000000.123,"area":2450}
```

Webhook вызывается не чаще одного раза в 60 секунд. Если URL не задан, события продолжают выводиться только в журнал add-on.

Проверка SSL-сертификата webhook включена по умолчанию. Для self-signed сертификата в конфигурации add-on можно установить `webhook_verify_ssl: false`. Используйте это только для доверенного endpoint, так как проверка подлинности сертификата будет отключена.

Для слабого компьютера начните со значений `width: 480` и `process_fps: 2`. Параметр `stream_timeout` задает время ожидания данных от камеры; после его истечения add-on переподключается. Для просмотра событий используйте вкладку **Log** add-on.

Сообщения `error while decoding MB` означают поврежденный H.264-пакет или некорректный кадр от камеры. Поврежденные пакеты отбрасываются, а после серии ошибок add-on пересоздает RTSP-соединение. Если ошибки повторяются, проверьте сетевое соединение и уменьшите битрейт/разрешение камеры.

```bash
docker compose down
```
