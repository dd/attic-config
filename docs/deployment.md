# Deployment

## Prerequisites

TrueNAS 25.10.7 (Linux-версия с systemd). CORE не поддерживается.


## Installation

1. Создать на пуле отдельный dataset, например `/mnt/POOL/system`, без SMB-шары и с владельцем root. Домашний каталог не подходит, потому что он принадлежит пользователю. Если dataset зашифрован, он должен быть разблокирован до Post Init, иначе после разблокировки нужно выполнить `sudo ./attic boot` вручную.
2. Родительские каталоги должны принадлежать root и быть закрыты на запись для группы и остальных. Setup это проверит, но не изменит. ACL setup не анализирует, поэтому не давать через них запись приложениям и пользователям.
3. Клонировать репозиторий и запустить setup.

```sh
cd /mnt/POOL/system
sudo git clone https://github.com/dd/attic-config.git
cd attic-config
sudo ./attic setup install
sudo ./attic setup status
```

Если репозиторий приватный, на NAS нужен доступ только на чтение, например deploy key. Токен не указывать в URL, иначе он сохранится в `.git/config`.

Setup делает владельцем всего содержимого папки root и убирает запись для группы и остальных. Символические ссылки, hard links и специальные файлы он отвергает. Если dataset не подходит, setup назовёт проблемный путь. Исправлять это нужно в настройках dataset, а не рекурсивной сменой прав на пуле.


## Post Init task

`setup install` создаёт задание, которое видно в **System → Advanced Settings → Init/Shutdown Scripts**.

| Field | Value |
| --- | --- |
| Description | attic-config: managed boot v1 |
| Type | Command |
| Command | `/usr/bin/python3 /mnt/POOL/system/attic-config/attic boot` |
| When | Post Init |
| Timeout | 120 seconds |

Повторный `setup install` обновляет то же задание и сохраняет его timeout. Изменить timeout можно командой `sudo ./attic setup install --timeout 240`. `attic boot` даёт каждому твику до 60 секунд, поэтому при нескольких твиках timeout задания стоит увеличить.

Задание хранится в конфигурации TrueNAS, а `local/` нужно резервировать вместе с dataset. Предполагается одна установка attic на NAS.

После обновления ОС твики со старой проверкой версии не установятся, и `attic status` покажет их включёнными, но не установленными. Перед изменением поддерживаемой версии нужно проверить твик на новой системе.


## Update

```sh
cd /mnt/POOL/system/attic-config
sudo git pull --ff-only
sudo ./attic setup install
sudo ./attic boot
./attic status
```

`local/` исключён из Git, поэтому pull не затрагивает настройки и список включённых твиков.

Если твик удалён из репозитория, но остался включённым, `boot` будет завершаться ошибкой. Исключить его из списка можно командой `sudo ./attic NAME uninstall`. Рабочие файлы такого твика исчезнут после перезагрузки.


## Uninstall

Команда `sudo ./attic NAME uninstall` удаляет один твик. Полностью убрать интеграцию можно так.

```sh
sudo ./attic setup uninstall
```

Setup отключит задание Post Init, удалит включённые твики и затем удалит само задание. Если удаление какого-то твика не удалось, задание остаётся отключённым, и команду можно повторить. Папка с кодом и `local/` не удаляются.


## Relocation

Переместить папку целиком вместе с `local/` в другой подходящий dataset и выполнить `sudo ./attic setup install` уже из нового места. Setup обновит путь в задании Post Init. Старой копией после этого не пользоваться.
