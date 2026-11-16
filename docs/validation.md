# Validation record

## Passed offline

Проверено 2026-11-02 на macOS.

- Offline-тесты (`python3 -B -m unittest discover -s tests`) на Python 3.10, 3.11 и 3.14.
- CLI `--help`, `list`, `status` и отказ менять систему на macOS.

systemd и middleware TrueNAS в тестах заменены mock-объектами.


## Passed on TrueNAS

Проверено 2026-11-16 на attic, TrueNAS 25.10.7, папка `/mnt/nvme1tb-system/system/attic-config`.

- `git clone`, `setup install` с реальным `midclt` (создано задание Post Init 1), проверка версии и прав.
- `list` и `status` без `sudo`.
- `avahi-recovery install`, после него status показывает `installed; Avahi=active; result=success; restarts=0`. Значит, drop-in применился и systemd видит `Restart=on-failure`.
- После перезагрузки status снова показывает `installed`, то есть задание Post Init `attic boot` восстанавливает твик.


## Pending on TrueNAS

Setup

- Проверить повторный `setup install`, `setup status` и `setup uninstall`.

Avahi

- По vendor unit и журналу реального сбоя подтвердить, что служба действительно падает и `Restart=on-failure` подходит.
- В тестовой VM или окне обслуживания проверить падение и перезапуск, исчерпание лимита и намеренный stop.
- Проверить, не сбрасывает ли `daemon-reload` счётчик лимита при `apply` и `boot`.
- Проверить, не конфликтует ли политика с управлением службой из middleware TrueNAS.
