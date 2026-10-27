# Deployment

## Prerequisites

Цель - Linux-based TrueNAS 25.10.7 с systemd и Python 3 (`/usr/bin/python3`). CORE не поддерживается. Проверить на NAS:

```sh
cat /etc/version
systemctl cat avahi-daemon.service
systemctl show avahi-daemon.service -p LoadState -p ActiveState -p Restart -p RestartMode -p OnFailure
journalctl -b -u avahi-daemon.service -n 100 --no-pager
```

Убедиться, что mDNS включён в настройках TrueNAS, служба не masked, а обнаружение действительно пропадает из-за перехода Avahi в `failed`. Не устанавливать обработчик вместо диагностики VLAN, multicast, firewall или SMB.


## Persistent source

1. Создать отдельный административный dataset, например `/mnt/POOL/admin`. Он должен быть доступен к моменту Post Init. Для зашифрованного dataset требуется разблокировка до запуска задания; иначе установка не выполнится, и её нужно запустить вручную после разблокировки.
2. Скопировать проверенную версию репозитория в `/mnt/POOL/admin/attic-config`. Для этого не требуется хранить GitHub credentials на NAS. Не выполнять автоматический `git pull` от root при загрузке.
3. Код, JSON и все родительские каталоги должны принадлежать root и быть недоступны для записи группе и остальным. Настроить также ACL dataset: скрипт проверяет POSIX mode bits, но не анализирует ACL. Не размещать код в SMB share или dataset, куда могут писать приложения или обычные пользователи.
4. Для уже созданного отдельного каталога с копией проекта можно выполнить:

```sh
sudo chown -R root:root /mnt/POOL/admin/attic-config
sudo chmod -R go-w /mnt/POOL/admin/attic-config
```

Не применять эти команды к целому пулу или существующим пользовательским данным. Проверить права родительского административного dataset отдельно. Символические ссылки в пути к скрипту, конфигу и рабочим файлам запрещены.


## Install

```sh
sudo /usr/bin/python3 /mnt/POOL/admin/attic-config/attic avahi-recovery install
```

Диспетчер сохраняет включение в `local/enabled.json`, а модуль Avahi создаёт:

- `/run/attic-avahi/avahi_recovery.py` и `config.json`: копии для текущей загрузки.
- `/run/systemd/system/avahi-daemon.service.d/90-attic-recovery.conf`: добавление `OnFailure` без сброса других обработчиков.
- `/run/systemd/system/attic-avahi-recovery.service`: ограниченный обработчик.
- `/run/attic-avahi/attempts.json`: общий счётчик попыток для текущей загрузки; появляется при первой попытке.

Повторная установка не дублирует hook и не обнуляет бюджет. Чужие файлы под зарезервированными именами вызывают отказ. При ошибке reload/проверки hook восстанавливаются предыдущие файлы. При ошибке самого отката нужно проверить журнал и файлы вручную. Установка не изменяет `/etc/avahi/avahi-daemon.conf`, vendor units, пакеты или read-only root filesystem.


## Reboot persistence

В **System > Advanced Settings > Init/Shutdown Scripts** добавить одно задание:

| Field | Value |
| --- | --- |
| Description | attic: restore enabled tweaks |
| Type | Command |
| Command | `/usr/bin/python3 /mnt/POOL/admin/attic-config/attic boot` |
| When | Post Init |
| Enabled | Yes |
| Timeout | 120 seconds |

В примере путь без пробелов; если они есть, заключить каждый путь в одинарные кавычки в Command. Не создавать несколько одинаковых заданий. Задание сохраняется через штатную конфигурацию TrueNAS; содержимое dataset резервируется отдельно. Экспорт конфигурации TrueNAS не заменяет резервную копию этих файлов.

`/run` очищается при загрузке. Post Init вызывает `attic boot`, который применяет только твики из `local/enabled.json`. Список включения и настройки должны сохраняться вместе с этой копией репозитория в dataset. Для одного Avahi указан timeout 120 секунд; при добавлении твиков увеличить его с учётом суммарного времени их установки. До выполнения задания защита не действует. Если Avahi уже `failed`, установка ставит обработчик в очередь; `inactive` намеренно не запускается. Не полагаться на точный порядок запуска всех служб относительно Post Init.

После обновления ОС не считать старый workaround автоматически совместимым. Проверка версии остановит повторную установку; после перезагрузки runtime hook отсутствует. Проверить новую версию, unit, необходимость workaround и поведение восстановления перед изменением version guard.


## Verify

```sh
systemctl cat avahi-daemon.service
systemctl cat attic-avahi-recovery.service
systemctl show avahi-daemon.service -p OnFailure -p ActiveState
systemd-analyze verify /run/systemd/system/attic-avahi-recovery.service
journalctl -b -u attic-avahi-recovery.service --no-pager
```

Повторить после перезагрузки. На macOS проверить `dns-sd -B _smb._tcp local.` (Ctrl-C для выхода) и `dns-sd -G v4v6 attic.local.`. Использовать фактическое имя NAS, если оно отличается. Состояние `active` само по себе не доказывает корректное обнаружение.

Проверять искусственное падение только в тестовой VM или согласованное окно: сначала убедиться, что Avahi active, затем `sudo systemctl kill --kill-whom=main --signal=SIGABRT avahi-daemon.service`. Это прерывает mDNS и может создать core dump. Наблюдать journal и фактическое повторное обнаружение. `systemctl stop` - проверка намеренной остановки, она не должна вызывать восстановление.


## Uninstall

Для удаления одного твика общее задание Post Init оставляем:

```sh
sudo /usr/bin/python3 /mnt/POOL/admin/attic-config/attic avahi-recovery uninstall
```

Команда сначала исключает Avahi из списка boot, затем удаление снимает trigger, останавливает обработчик и удаляет собственные runtime unit/скрипт/конфиг. Avahi не останавливается и не перезапускается; другие drop-ins остаются. Повторное удаление допустимо. История попыток и lock-файлы остаются до перезагрузки, чтобы переустановка не обходила лимит. Каталог с исходниками в dataset остаётся. Для полного отказа от всех дополнений удалить каждый через uninstall, затем отключить общее задание Post Init. Отключённое задание и перезагрузка также убирают runtime-интеграцию Avahi.

После устранения причины постоянного сбоя можно вручную выполнить `sudo systemctl reset-failed avahi-daemon.service` и `sudo systemctl start avahi-daemon.service`. Обработчик не содержит таймера: после исчерпания бюджета он не проснётся сам по истечении окна. Новый сбой или ручной запуск обработчика после истечения окна может инициировать следующую ограниченную попытку.


## Configure and update

```sh
sudo /usr/bin/python3 /mnt/POOL/admin/attic-config/attic avahi-recovery config
sudo /usr/bin/python3 /mnt/POOL/admin/attic-config/attic avahi-recovery apply
sudo /usr/bin/python3 /mnt/POOL/admin/attic-config/attic avahi-recovery status
```

`config` открывает временную копию JSON в `$EDITOR` (по умолчанию `vi`). Можно указать редактор явно: `sudo EDITOR=nano ./attic avahi-recovery config`. Неверный конфиг не сохраняется. Для включённого твика валидные изменения сразу применяются; для отключённого только сохраняются, без установки. Если apply завершается ошибкой, config восстанавливает сохранённый конфиг, а установщик Avahi откатывает собственные рабочие файлы.

`apply` нужен после ручной правки `tweaks/avahi-recovery/config.json` или обновления кода; он не включает отключённый твик. Применение меняет runtime-копию настроек, но уже выполняющийся обработчик может завершить попытку со старыми значениями. Следующие запуски используют новый конфиг.

При обновлении исходников сохранять `local/` и локальные изменения `tweaks/*/config.json`. После ручного редактирования неверный файл остаётся на диске, и apply откажется его использовать. Исправить файл перед перезагрузкой. Никогда не перезаписывать список включённых твиков состоянием с другого сервера.


## Migration from the initial layout

Если старая версия уже была установлена на NAS:

1. Отключить старое задание Post Init, которое вызывало `scripts/avahi_recovery.py`.
2. Сохранить изменённый `config/recovery.json`, обновить копию репозитория и перенести значения в `tweaks/avahi-recovery/config.json`.
3. Выполнить `attic avahi-recovery install`, чтобы записать включение и обновить runtime-файлы. Их имена и ownership marker сохранены, поэтому удалять работающую интеграцию предварительно не требуется.
4. Заменить старую команду Post Init на `attic boot`, включить задание и проверить `attic status`.

Не оставлять одновременно старое и новое задания. Старые management-команды скрипта заменены интерфейсом `attic`; обычное управление выполнять через `attic`, чтобы сохранялось включение. Systemd по-прежнему запускает скрипт напрямую с внутренней командой `recover`.

Если использовалась промежуточная модульная версия с `config/avahi-recovery.json`, перенести её настройки в `tweaks/avahi-recovery/config.json` перед `attic avahi-recovery apply`. Команды управления и задание `attic boot` остаются прежними.

После перехода с интерфейса `attic install NAME` использовать `attic NAME install`; настройка теперь вызывается через `attic NAME config`. Сохранённый `local/enabled.json`, расположение конфигов и команда Post Init `attic boot` не меняются. Старый порядок аргументов больше не поддерживается.
