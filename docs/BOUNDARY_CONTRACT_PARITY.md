# Contract parity Stream Dock boundary

Этап 7 проверяет, что legacy `WebSocketStreamDockConnection` и экспериментальная
`_next` boundary дают одинаковый внешний результат до подключения runtime.
Проверки используют fake transport, поэтому не требуют работающего Stream Dock.

## Контрактные проверки

Запуск из checkout:

```bash
PYTHONPATH=src python -m unittest tests.next.test_contract_parity -v
```

Набор проверяет обе реализации одним и тем же входом:

- точный wire output всех известных команд;
- доставку `UnknownStreamDockEvent` и отбрасывание malformed frames;
- FIFO входящих событий и исходящих команд;
- coalescing, queue-full и нормализованные queue metrics;
- успешный graceful drain и terminal failure после bounded timeout;
- terminal completion каждой принятой команды и rejection после shutdown.

### Получение событий в `_next`

`receive()` переводит событие в состояние `in_flight`. После завершения всей
обработки потребитель обязан ровно один раз вызвать `task_done()`. Вызывать его
нужно в `finally`, причём сам `receive()` должен оставаться перед `try`: если
получение завершилось ошибкой, подтверждать нечего.

```python
while running:
    event = boundary.events.receive()
    try:
        handle_event(event)
    finally:
        boundary.events.task_done()
```

Graceful shutdown ждёт такие подтверждения до
`BoundaryShutdownConfig.inbound_event_drain_timeout` (по умолчанию пять секунд).
Значение `None` означает неограниченное ожидание, поэтому пропущенный
`task_done()` в этом режиме не позволит закрытию завершиться.

## Benchmark

```bash
PYTHONPATH=src python scripts/benchmark_boundary.py --iterations 5 --check
```

Скрипт выводит JSON с медианой для legacy и `_next` в сценариях:

- burst `keyDown`/`keyUp`;
- интенсивный `dialRotate`;
- большие `setImage`;
- конкурентные `setTitle`;
- slow consumer;
- slow WebSocket send;
- disconnect при заполненных queues.

Поля `latency_ms` и `throughput_per_second` измеряют весь сценарий. Поля
`net_allocation_blocks` и `net_allocated_bytes` являются полной signed-разницей
между начальным и конечным snapshot `tracemalloc`. Они показывают retained delta,
а не суммарное число краткоживущих allocations. `peak_traced_bytes` показывает
пиковую traced memory и используется как allocation-pressure guardrail.

Legacy запускается непосредственно перед `_next` в том же процессе и является
исполняемым baseline для каждого сценария. JSON содержит секцию `comparison` с
ratios, численными лимитами, причиной разрешённого overhead и результатом
`within_budget`. Порядок реализаций чередуется между итерациями, чтобы уменьшить
warm-cache bias. Флаг `--check` возвращает non-zero exit code при превышении
бюджета. Абсолютные числа не хранятся в репозитории, поскольку зависят от Python,
ОС и железа; версия Python и платформа входят в JSON.

Fake benchmark не моделирует session coordinator: final `Disconnected` закрывается
с timeout `0`, чтобы невостребованный lifecycle event не искажал latency рабочих
сценариев. Shutdown-сценарий по-прежнему измеряет явный bounded disconnect.

## Осознанные различия

`_next` намеренно добавляет raw и typed очереди, а также reader/writer workers.
Поэтому абсолютные allocation и peak-depth отдельных очередей не обязаны
совпадать с legacy. Для `_next` `peak_queue_depth` — максимум по relevant stages,
и каждая стадия обязана оставаться в собственном limit.

Дополнительные handoff между очередями и `TransportReceipt` повышают latency и
allocation pressure в burst, конкурентных outbound и disconnect-сценариях. Это
принятая цена за API-independent transport, bounded backpressure и terminal
completion каждой принятой команды. Поэтому эти сценарии сравниваются с legacy
как guardrail: заметное ухудшение требует объяснения, но не отменяет эту
архитектурную гарантию молча.

| Сценарий | Max latency ratio | Max peak traced bytes ratio | Причина бюджета |
|---|---:|---:|---|
| burst `keyDown`/`keyUp` | 1.50 | 1.50 | Дополнительный raw-to-typed inbound handoff |
| интенсивный `dialRotate` | 1.50 | 1.75 | Coalescing выполняется после последовательного decode |
| большие `setImage` | 1.50 | 3.00 | Raw queue временно удерживает encoded image frames |
| конкурентные `setTitle` | 1.75 | 2.00 | Per-command completion state |
| slow consumer | 1.30 | 1.50 | Raw backpressure изолирует WebSocket reader |
| slow WebSocket send | 1.30 | 3.00 | Pending receipts остаются наблюдаемыми |
| disconnect с заполненными queues | 5.50 | 3.50 | Все стадии явно drain/fail незавершённую работу |

Бюджеты являются guardrail, а не целью оптимизации. Их изменение требует
обновить причину в коде и в этой таблице; `--check` не позволяет принять новое
ухудшение только из-за обновления измеренных абсолютных чисел.

Оценивать parity нужно по wire output, terminal completion и нормализованным
счётчикам accepted/serialized/sent. Рост latency, allocations или aggregate peak
queue depth требует зафиксировать причину в изменении boundary до включения
`_next` в runtime.

## Матрица паритета legacy runtime

Эта матрица является baseline для Runtime Dispatcher. Статус «зафиксирован»
означает, что observable expectation и доступное evidence записаны; он не
означает, что новый runtime уже реализован. Пробелы прямого regression coverage
отмечены явно. Строка закрывается только после появления эквивалентного
`_next/runtime` contract или integration test.

| Observable contract | Baseline evidence | Требование к Runtime Dispatcher | Статус этапа 0 |
|---|---|---|---|
| Registration и initial global settings request | `test_registers_and_dispatches_events_without_plugin_specific_code` | Обе команды завершаются до первого application callback | Зафиксирован |
| Создание action на `willAppear` | тот же runtime test | Instance сохранён до `on_will_appear` | Зафиксирован |
| Rollback неуспешного appearance | `test_rolls_back_action_when_appearance_fails` | Удаляется именно новый instance, cleanup вызывается один раз | Зафиксирован |
| Удаление на `willDisappear` и shutdown cleanup | `ActionStore.remove_and_dispatch`; shutdown cleanup покрыт `test_starts_services_and_stops_them_in_reverse_order_once` | Context удалён до callback; terminal cleanup получает `None` | Нужен прямой `willDisappear` regression test на этапе 3 |
| Action-scoped routing и неизвестный context | `ActionStore.dispatch`; happy path покрыт runtime dispatch test | Событие получает только текущий instance; missing context не создаёт action | Missing-context coverage добавляется на этапе 3 |
| Broadcast routing и failure isolation | `test_broadcast_failure_does_not_block_other_actions` | Один stable snapshot; failure одного action не блокирует остальные | Зафиксирован |
| Forward-compatible unknown event | `test_delivers_unknown_event_to_plugin_hook_once` | Один plugin-scope hook без action broadcast | Зафиксирован |
| Settings/title state before callback | `ActionStore.update_settings_and_dispatch` и `update_title_and_dispatch`; codec tests покрывают validation | Callback видит только полностью обновлённое состояние | Нужны прямые state-before-callback tests на этапе 3 |
| Global settings snapshot, replay и JSON isolation | `test_replays_*`, `test_isolates_global_settings_*` | Последний snapshot replay-ится поздним actions изолированным view | Зафиксирован |
| Callback failure isolation | `test_records_callback_failures_without_stopping_dispatch` | Failure завершает только текущий outcome и наблюдается в metrics | Зафиксирован |
| Ordering одного context и global barriers | `test_does_not_overlap_callbacks_for_same_context`, `test_lifecycle_and_broadcast_events_are_exclusive_barriers` | Context serial; lifecycle и broadcast эксклюзивны | Зафиксирован |
| Close из callback и bounded shutdown | callback-close и shutdown-timeout tests в `tests/test_sdk.py` | Нет deadlock; pending work получает явный terminal outcome | Зафиксирован |
| Команды из callbacks до outbound shutdown | `tests/next/test_boundary_composition.py` и experimental integration | Accepted callback commands получают terminal completion до outbound close | Зафиксирован |
| Произвольные `LifecycleService` | service lifecycle tests в `tests/test_runtime.py` | Переносятся в application host; не являются обязанностью dispatcher-а | Ownership зафиксирован |

Допустимое отличие этапа миграции: legacy plugin в данный момент сам владеет
`LifecycleService`, тогда как новая композиция переносит это владение в
application host. Порядок запуска services относительно session readiness будет
зафиксирован отдельно до переключения default runtime.
