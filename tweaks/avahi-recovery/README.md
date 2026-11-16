# Avahi recovery design

## Usage

Управление из корня репозитория:

```sh
sudo ./attic avahi-recovery install
sudo ./attic avahi-recovery config
sudo ./attic avahi-recovery apply
./attic avahi-recovery status
sudo ./attic avahi-recovery uninstall
```

Значения по умолчанию лежат в [`config.json`](config.json). `config` редактирует их через `$EDITOR` и сохраняет отличия в `local/config/avahi-recovery.json`. Для включённого твика валидные изменения сразу применяются; для отключённого только сохраняются. Если применение завершается ошибкой, прежние настройки и drop-in восстанавливаются. Редактор можно указать явно, например `sudo EDITOR=nano ./attic avahi-recovery config`.


## Scope and evidence

Документация проверена 2026-10-27. TrueNAS 25.10.7 опубликована 2026-09-02 согласно [официальным release notes](https://www.truenas.com/docs/scale/25.10/gettingstarted/scalereleasenotes/). Сам факт этой версии не доказывает наличие конкретного дефекта Avahi. Доступный экспорт предыдущего чата не содержит логов сбоя или текста предложенного решения. Поэтому репозиторий не приписывает падение конкретному upstream issue и не утверждает, что причина устранена.

Это временная мера для случая, когда `avahi-daemon.service` падает, а повторный запуск восстанавливает mDNS. Она не проверяет ответы DNS-SD, не лечит зависшую `active` службу и не гарантирует появление NAS в Finder.

Перед установкой убедиться, что mDNS включён в настройках TrueNAS, служба не masked, а обнаружение действительно пропадает из-за падения Avahi. Не устанавливать твик вместо диагностики VLAN, multicast, firewall или SMB.


## Failure handling

Твик создаёт один файл `/run/systemd/system/avahi-daemon.service.d/90-attic-recovery.conf`. Собственного обработчика, таймера и счётчика попыток нет, перезапуском и лимитом управляет systemd.

| Setting | systemd | Default | Bounds |
| --- | --- | --- | --- |
| `attempts` | `StartLimitBurst` | 3 | 1-3 |
| `window_seconds` | `StartLimitIntervalSec` | 600 | 300-86400 |
| `delay_seconds` | `RestartSec` | 20 | 5-60 |

`Restart=on-failure` срабатывает при ненулевом коде выхода, падении по сигналу и timeout. Ручной `systemctl stop` и остановка системы перезапуск не вызывают. Лимит считает все запуски службы в окне, включая обычный, поэтому если Avahi падает вскоре после старта, автоматических перезапусков будет на один меньше.

После исчерпания лимита служба остаётся в `failed`, а status показывает `result=start-limit-hit`. Бесконечного повтора нет; нужна диагностика. После устранения причины:

```sh
sudo systemctl reset-failed avahi-daemon.service
sudo systemctl start avahi-daemon.service
```

Установка проверяет, что политика действительно применилась, и откатывает файл, если другой drop-in её перекрывает. Если Avahi уже `failed`, установка один раз запускает службу без сброса лимита, поэтому повторные `apply` и `boot` лимит не обходят. Службу в `inactive` установка не запускает. Vendor unit, `/etc/avahi/avahi-daemon.conf` и пакеты не меняются. Чужой файл с тем же именем вызывает отказ.

`uninstall` удаляет drop-in и выполняет `daemon-reload`; Avahi не останавливается и не перезапускается, другие drop-ins остаются. Без задания Post Init drop-in исчезает после перезагрузки.


## Verify

```sh
systemctl cat avahi-daemon.service
systemctl show avahi-daemon.service -p DropInPaths -p Restart -p RestartUSec -p StartLimitBurst -p StartLimitIntervalUSec
journalctl -b -u avahi-daemon.service --no-pager
```

Повторить после перезагрузки. На macOS проверить `dns-sd -B _smb._tcp local.` (Ctrl-C для выхода) и `dns-sd -G v4v6 attic.local.`. Использовать фактическое имя NAS, если оно отличается. Состояние `active` само по себе не доказывает корректное обнаружение.

Проверять искусственное падение только в тестовой VM или в согласованное окно. Сначала убедиться, что Avahi active, затем выполнить `sudo systemctl kill --kill-whom=main --signal=SIGABRT avahi-daemon.service`. Это прерывает mDNS и может создать core dump. Наблюдать journal и фактическое повторное обнаружение. `systemctl stop` - проверка намеренной остановки, она не должна вызывать восстановление.


## Interface names

В исходниках [Avahi `iface.c`](https://github.com/avahi/avahi/blob/master/avahi-core/iface.c) allow/deny lists проверяются через `strcasecmp` с полным именем интерфейса. `veth*`, `br-*` и другие glob patterns не поддерживаются.

Репозиторий не добавляет allow/deny lists. Если диагностика позднее подтвердит необходимость ограничения интерфейсов, сначала определить реальный LAN-интерфейс (`ip -brief address`), мост или VLAN. Например, `allow-interfaces=br0` имеет смысл только если LAN действительно находится на `br0`. Не исключать все bridge-интерфейсы по названию, потому что среди них может быть основной LAN.

Изменение генерируемого TrueNAS конфига вручную может быть перезаписано системой. Для отдельного решения с фильтрацией сначала проверить механизм генерации именно установленной версии. Этот проект не выдаёт неподтверждённый способ сохранения такой правки за рабочий.


## Diagnosis

Перед восстановлением или изменением настроек сохранить локально, вне Git:

```sh
systemctl status avahi-daemon.service --no-pager
systemctl show avahi-daemon.service -p Result -p ExecMainStatus -p NRestarts -p Restart
journalctl -b -u avahi-daemon.service -n 200 --no-pager
ip -brief address
```

Сопоставить время ошибки с запуском Apps, изменениями интерфейсов, сетью и перезагрузками. Если Avahi active, проверить mDNS/UDP 5353, multicast между сегментами и публикацию SMB. Не отключать IPv6 и не менять сетевые интерфейсы без подтверждённой причины.


## References

- [TrueNAS 25.10 release notes](https://www.truenas.com/docs/scale/25.10/gettingstarted/scalereleasenotes/) — версия и дата релиза.
- [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html) — Restart и RestartSec. Документация latest; поведение установленной версии проверять локально.
- [systemd.unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html) — load paths и start limits.
- [Avahi configuration reference](https://manpages.debian.org/unstable/avahi-daemon/avahi-daemon.conf.5.en.html) — allow/deny lists.
- [Avahi source](https://github.com/avahi/avahi/blob/master/avahi-core/iface.c) — сравнение точных имён. Ссылка на текущую ветку, не подтверждение версии Avahi, поставляемой с конкретным NAS.
