# Validation record

Проверено 2026-11-02 на macOS, без подключения к NAS. **На реальном NAS решение ещё не запускалось.**


## Passed

- Offline-тесты (`python3 -B -m unittest discover -s tests`) на Python 3.10, 3.11 и 3.14.
- CLI `--help`, `list`, `status` и отказ менять систему на macOS.

systemd и middleware TrueNAS в тестах заменены mock-объектами.


## Pending on TrueNAS

Setup

- Проверить `git clone` и `git pull` на NAS.
- Проверить фактический формат `/etc/version`. Код принимает `TrueNAS-SCALE-25.10.7`, `TrueNAS-25.10.7` и `25.10.7`.
- Проверить `attic setup install/status/uninstall` с реальным `midclt` и повторный запуск setup.
- Проверить, что `chmod` проходит на выбранном dataset.
- Проверить, что задание `attic boot` срабатывает после перезагрузки.

Avahi

- По vendor unit и журналу реального сбоя подтвердить, что служба действительно падает и `Restart=on-failure` подходит.
- После `install` проверить status и применённые `Restart`, `RestartUSec`, `StartLimitBurst`, `StartLimitIntervalUSec`.
- В тестовой VM или окне обслуживания проверить падение и перезапуск, исчерпание лимита и намеренный stop.
- Проверить, не сбрасывает ли `daemon-reload` счётчик лимита при `apply` и `boot`.
- Проверить, не конфликтует ли политика с управлением службой из middleware TrueNAS.
