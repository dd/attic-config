# attic-config

Дополнения (твики) для NAS **attic** на TrueNAS 25.10.7, управляемые одной командой `attic`. Каждый твик — отдельный скрипт в `tweaks/NAME/` со своими настройками, документацией и тестами.


## First installation

Клонировать в отдельный dataset на пуле, **без** SMB-шары. Домашний каталог не подходит - код конфигуратора твиков запускается от root при каждой загрузке, и setup требует, чтобы весь путь до папки принадлежал root.

```sh
cd /mnt/POOL/system
sudo git clone https://github.com/dd/attic-config.git
cd attic-config
sudo ./attic setup install
sudo ./attic avahi-recovery install
./attic status
```

`setup install` закрепляет за root права на папку и создаёт в TrueNAS задание Post Init `attic boot` (видно в **System → Advanced Settings → Init/Shutdown Scripts**). В систему ничего не копируется, `pip` и `venv` не нужны. Твики включаются только явно, через `NAME install`.


## How it works

Твики не меняют постоянные системные файлы, а пишут свои изменения в `/run`, который очищается при перезагрузке. При каждой загрузке задание Post Init вызывает `attic boot`, и тот заново применяет включённые твики. Если папка пропадёт, NAS просто загрузится без твиков.


## Commands

| Command | Effect |
| --- | --- |
| `setup install` | Настроить права и задание Post Init; повторять после обновления или переноса папки |
| `setup status` | Показать задание Post Init |
| `setup uninstall` | Удалить включённые твики и задание Post Init; файлы и настройки остаются |
| `list` | Показать доступные твики |
| `status` | Показать, какие твики включены и работают |
| `boot` | Применить все включённые твики (это и вызывает Post Init) |
| `NAME install` | Установить и включить твик |
| `NAME uninstall` | Выключить и удалить твик |
| `NAME config` | Редактировать настройки твика |
| `NAME apply` | Применить изменённые настройки или код |
| `NAME status` | Показать состояние твика |
| `NAME --help` | Справка твика, в том числе его собственные команды |

`list`, `status` и справка работают без `sudo`, остальное требует root на NAS.


## Tweaks

| Tweak | Purpose |
| --- | --- |
| [avahi-recovery](tweaks/avahi-recovery/README.md) | Ограниченный автоматический перезапуск Avahi после падения |

Настройки по умолчанию лежат в `tweaks/NAME/config.json`, а изменённые через `NAME config` значения — в `local/config/NAME.json`. Список включённых твиков хранится в `local/enabled.json`. `local/` не входит в Git, поэтому обновление не затрагивает настройки.


## Update

```sh
cd /mnt/POOL/system/attic-config
sudo git pull --ff-only
sudo ./attic setup install
sudo ./attic boot
```


## Development

Только стандартная библиотека Python 3.10+.

```sh
python3 -B -m unittest discover -s tests -v
```

Тесты твиков лежат в `tweaks/*/tests` и запускаются этой же командой. Новый твик — это новая папка в `tweaks/`, диспетчер менять не нужно.

Подробнее в документах [установка](docs/deployment.md), [контракт твика](docs/modules.md), [результаты проверок](docs/validation.md).

Не хранить в репозитории пароли, токены, ключи, экспорты конфигурации TrueNAS и журналы с личными данными.
