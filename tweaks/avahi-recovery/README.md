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

Настройки: [`config.json`](config.json). Модуль проверяет значения, редактирует конфиг через `$EDITOR` и устанавливает рабочие файлы. Диспетчер отвечает за выбор модуля и список включения. [Инструкция установки и Post Init](../../docs/deployment.md).


## Scope and evidence

Документация проверена 2026-10-27. TrueNAS 25.10.7 опубликована 2026-09-02 согласно [официальным release notes](https://www.truenas.com/docs/scale/25.10/gettingstarted/scalereleasenotes/). Сам факт этой версии не доказывает наличие конкретного дефекта Avahi. Доступный экспорт предыдущего чата не содержит логов сбоя или текста предложенного решения. Поэтому репозиторий не приписывает падение конкретному upstream issue и не утверждает, что причина устранена.

Это временная мера для случая, когда `avahi-daemon.service` переходит в `failed`, а повторный запуск восстанавливает mDNS. Она не проверяет ответы DNS-SD, не лечит зависшую `active` службу и не гарантирует появление NAS в Finder.


## Failure handling

`OnFailure` добавляется только к Avahi. Обработчик не имеет собственного `OnFailure` и не связан с Avahi через `Requires`, `PartOf` или `After`: это исключает лишние зависимости при восстановлении. Vendor `Restart` остаётся без изменений. Если vendor перезапускает службу самостоятельно, handler запускается при достижении `failed`, в том числе после исчерпания штатного start limit. `RestartMode=direct` несовместим и проверяется установщиком.

Обработчик проверяет состояние system manager и Avahi, ждёт cooldown и повторяет проверку. Состояния `inactive`, `deactivating` и остановка системы не инициируют восстановление. Затем он записывает попытку в общий бюджет, сбрасывает failed/start-limit Avahi и ставит `start --no-block` в очередь. Результат опрашивается до 15 секунд; немедленное повторное падение обрабатывается внутри того же запуска. Отдельные запуски разделяют бюджет и lock. Сброс штатного start limit не снимает собственный лимит проекта.

По умолчанию допускаются три попытки за 600 секунд с задержкой 20 секунд. Лимит конфигурации - максимум три попытки, задержка 5-60 секунд, окно 300-86400 секунд. Тайм-аут systemd дополнительно ограничивает длительность handler. Ошибки остаются в journal. После исчерпания попыток требуется диагностика; бесконечного фонового retry нет.

Проверка состояния и постановка start в очередь не являются атомарными: ручная остановка строго в этот короткий промежуток может пересечься с восстановлением. Перед обслуживанием отключить интеграцию через uninstall. В случае очень короткого повторного падения на границе завершения handler системный `OnFailure` тоже может не создать новый запуск уже активного oneshot; это не полноценный watchdog. После установки и обновлений нужен тест на реальной версии systemd.


## Interface names

В исходниках [Avahi `iface.c`](https://github.com/avahi/avahi/blob/master/avahi-core/iface.c) allow/deny lists проверяются через `strcasecmp` с полным именем интерфейса. `veth*`, `br-*` и другие glob patterns не поддерживаются.

Репозиторий не добавляет allow/deny lists. Если диагностика позднее подтвердит необходимость ограничения интерфейсов, сначала определить реальный LAN-интерфейс (`ip -brief address`), мост или VLAN. Например, `allow-interfaces=br0` имеет смысл только если LAN действительно находится на `br0`. Не исключать все bridge-интерфейсы по названию: среди них может быть основной LAN.

Изменение генерируемого TrueNAS конфига вручную может быть перезаписано системой. Для отдельного решения с фильтрацией сначала проверить механизм генерации именно установленной версии. Этот проект не выдаёт неподтверждённый способ сохранения такой правки за рабочий.


## Diagnosis

Перед восстановлением или изменением настроек сохранить локально, вне Git:

```sh
systemctl status avahi-daemon.service --no-pager
systemctl show avahi-daemon.service -p Result -p ExecMainStatus -p NRestarts -p Restart
journalctl -b -u avahi-daemon.service -n 200 --no-pager
journalctl -b -u attic-avahi-recovery.service -n 100 --no-pager
ip -brief address
```

Сопоставить время ошибки с запуском Apps, изменениями интерфейсов, сетью и перезагрузками. Если Avahi active, проверить mDNS/UDP 5353, multicast между сегментами и публикацию SMB. Не отключать IPv6 и не менять сетевые интерфейсы без подтверждённой причины.


## References

- [TrueNAS 25.10 release notes](https://www.truenas.com/docs/scale/25.10/gettingstarted/scalereleasenotes/): версия и дата релиза.
- [TrueNAS 25.10 Advanced Settings](https://www.truenas.com/docs/scale/25.10/scaleuireference/systemsettings/advancedsettingsscreen/): штатные Init/Shutdown tasks.
- [TrueNAS 25.10 API: initshutdownscript](https://api.truenas.com/v25.10.0/api_events_initshutdownscript.query.html): POSTINIT и timeout.
- [systemd.unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html): OnFailure, load paths и start limits. Документация latest; поведение установленной версии проверять локально.
- [Avahi configuration reference](https://manpages.debian.org/unstable/avahi-daemon/avahi-daemon.conf.5.en.html): allow/deny lists.
- [Avahi source](https://github.com/avahi/avahi/blob/master/avahi-core/iface.c): сравнение точных имён. Ссылка на текущую ветку, не подтверждение версии Avahi, поставляемой с конкретным NAS.
