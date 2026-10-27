"""E2E-тесты handle() фазы 4 (UX-итерация): Router, дедуп в БД, джоба,
имя треда в payload — без сообщений в чат и без создания треда.

Контракт (docs/ARCHITECTURE.md §6, §10 + UX-итерация):
- route = deps.router.route(message.content): совпадение ищут экстракторы
  (find -> границы); None — в сообщении нет ссылки, никаких действий;
- дедуп: deps.dedup.mark(str(message.id), extractor.canonical_id(route.url)) —
  False (уже обработано) -> выход. Дедуп в БД: переживает рестарт, ключ —
  сообщение, а не canonical (та же ссылка в другом сообщении разбирается);
- бот молчит: ни reply-сообщения «разбираю», ни статусных правок, ни треда —
  тред создаёт воркер (process_job), когда разбор уже готов;
- джоба: kind == extractor.name, url — совпавшая подстрока, canonical_id от
  экстрактора, channel_id/message_id исходного сообщения, thread_name,
  display-метаданные, job_id (вернул deps.jobs.put). thread_id у новой джобы
  нет — его не существует до готового разбора;
- имя треда (thread_name): «Название (Автор)» из describe; иначе текст
  сообщения без ссылки (срез route.start:route.end); если после обрезки пусто —
  f"Разбор {route.url}"[:90].
"""

import asyncio

from bot.discord_app import handle

VIDEO_ID = "abcdefghijk"
URL = f"https://youtu.be/{VIDEO_ID}"


class TestRoutingAndJob:
    def test_link_enqueues_single_job_with_new_format(
        self, make_deps, make_message, make_thread, make_extractor, make_router
    ):
        extractor = make_extractor()
        thread = make_thread(555)
        deps = make_deps(router=make_router([extractor]))
        msg = make_message(f"глянь {URL}", thread=thread, status_id=999)

        asyncio.run(handle(msg, deps))

        job = asyncio.run(deps.queue.get())
        assert job["kind"] == extractor.name == "video"
        assert job["url"] == URL, "url джобы — совпавшая подстрока сообщения"
        assert job["canonical_id"] == extractor.canonical_id(URL)
        assert job["channel_id"] == msg.channel.id
        assert job["message_id"] == msg.id
        assert "thread_id" not in job, "треда ещё нет — его создаёт воркер"
        assert "status" not in job and "status_message_id" not in job, (
            "статусных сообщений больше нет"
        )
        assert isinstance(job["thread_name"], str) and job["thread_name"]
        assert isinstance(job["job_id"], int)
        assert deps.queue.empty()

    def test_handle_writes_nothing_to_chat_and_no_thread(
        self, make_deps, make_message, make_thread, make_extractor, make_router
    ):
        # UX-контракт: бот молчит до готового разбора — ни reply «разбираю…»,
        # ни треда под сообщением (тред создаёт воркер вместе с разбором)
        extractor = make_extractor()
        deps = make_deps(router=make_router([extractor]))
        msg = make_message(f"глянь {URL}")

        asyncio.run(handle(msg, deps))

        assert msg.replies == [], "промежуточное сообщение в чате не пишется"
        assert msg.status.edits == [] and not msg.status.deleted
        assert msg.thread_kwargs is None, "тред не создаётся в handle"

    def test_repeat_of_same_message_deduplicated(
        self, make_deps, make_message, make_thread, make_extractor, make_router
    ):
        extractor = make_extractor()
        deps = make_deps(router=make_router([extractor]))
        msg = make_message(f"глянь {URL}")

        asyncio.run(handle(msg, deps))
        asyncio.run(handle(msg, deps))  # повтор того же сообщения — дедуп

        asyncio.run(deps.queue.get())  # слить легитимную джобу первого handle
        assert deps.queue.empty(), "дедуп не поставил вторую джобу"
        assert deps.jobs.count_new() == 1

    def test_same_link_in_other_message_is_processed(
        self, make_deps, make_message, make_thread, make_extractor, make_router
    ):
        # дедуп по сообщению, не по canonical: та же ссылка в другом сообщении
        # создаёт свою джобу
        deps = make_deps(router=make_router([make_extractor()]))
        first = make_message(f"глянь {URL}", id=3001, thread=make_thread(555))
        second = make_message(f"глянь {URL}", id=3002, thread=make_thread(556))

        asyncio.run(handle(first, deps))
        asyncio.run(handle(second, deps))

        assert deps.jobs.count_new() == 2

    def test_dedup_survives_restart(
        self, make_deps, make_message, make_thread, make_extractor, make_router, tmp_path
    ):
        # дедуп в БД: новый Deps на той же базе отбрасывает уже обработанное сообщение
        from bot.store.db import Store

        store = Store(str(tmp_path / "dedup.sqlite3"))
        try:
            router = make_router([make_extractor()])
            deps1 = make_deps(router=router, jobs=store.jobs, dedup=store.dedup)
            msg = make_message(f"глянь {URL}", id=3003)
            asyncio.run(handle(msg, deps1))
            assert deps1.jobs.count_new() == 1

            deps2 = make_deps(router=router, jobs=store.jobs, dedup=store.dedup)
            asyncio.run(handle(msg, deps2))  # «после рестарта», тот же message.id

            assert deps2.queue.empty(), "дедуп из БД пережил рестарт"
            assert deps2.jobs.count_new() == 1
        finally:
            store.close()

    def test_plain_message_no_job_no_dedup_no_thread(
        self, make_deps, make_message, make_extractor, make_router, make_dedup_store
    ):
        deps = make_deps(router=make_router([make_extractor()]), dedup=make_dedup_store())
        msg = make_message("просто текст, без ссылок")

        asyncio.run(handle(msg, deps))

        assert deps.queue.empty()
        assert deps.jobs.count_new() == 0
        assert deps.dedup.mark_calls == [], "без совпадения дедуп не трогается"
        assert msg.thread_kwargs is None, "тред не создаётся"
        assert msg.replies == [] and msg.status.edits == []

    def test_job_payload_in_store_has_thread_breadcrumbs(
        self, make_deps, make_message, make_thread, make_extractor, make_router
    ):
        # джоба в БД самодостаточна для треда: channel_id/message_id исходного
        # сообщения + thread_name + display-метаданные; статусных полей нет
        extractor = make_extractor(describe_result=("Название", "Автор"))
        deps = make_deps(router=make_router([extractor]))
        msg = make_message(f"глянь {URL}")

        asyncio.run(handle(msg, deps))

        rows = deps.jobs.take_new()
        assert len(rows) == 1
        stored = rows[0]
        queued = asyncio.run(deps.queue.get())
        assert stored["job_id"] == queued["job_id"], (
            "джоба в очереди знает свой id в БД"
        )
        assert stored["channel_id"] == msg.channel.id
        assert stored["message_id"] == msg.id
        assert stored["thread_name"] == "Название (Автор)"
        assert stored["display_title"] == "Название" and stored["display_author"] == "Автор"
        assert "status" not in stored and "status_message_id" not in stored
        assert stored["url"] == URL
        assert stored["canonical_id"] == extractor.canonical_id(URL)


class TestThreadName:
    def test_strips_link_keeps_text(self, make_deps, make_message, make_extractor, make_router):
        deps = make_deps(router=make_router([make_extractor()]))
        msg = make_message(f"смотрите {URL} это интересно")

        asyncio.run(handle(msg, deps))

        job = asyncio.run(deps.queue.get())
        name = job["thread_name"]
        assert "youtu" not in name and VIDEO_ID not in name
        assert "смотрите" in name and "это интересно" in name

    def test_fallback_when_only_link(self, make_deps, make_message, make_extractor, make_router):
        deps = make_deps(router=make_router([make_extractor()]))
        msg = make_message(URL)

        asyncio.run(handle(msg, deps))

        job = asyncio.run(deps.queue.get())
        assert job["thread_name"] == f"Разбор {URL}"[:90]

    def test_truncated_to_90(self, make_deps, make_message, make_extractor, make_router):
        deps = make_deps(router=make_router([make_extractor()]))
        msg = make_message("т" * 300 + " " + URL)

        asyncio.run(handle(msg, deps))

        job = asyncio.run(deps.queue.get())
        assert len(job["thread_name"]) <= 90

    def test_describe_names_thread_without_link(
        self, make_deps, make_message, make_extractor, make_router
    ):
        # UX-итерация: имя треда = «Название (Автор)», ссылок в имени нет
        extractor = make_extractor(describe_result=("Призмы анимо: главный секрет", "Мир Aniimo"))
        deps = make_deps(router=make_router([extractor]))
        msg = make_message(URL)

        asyncio.run(handle(msg, deps))

        job = asyncio.run(deps.queue.get())
        name = job["thread_name"]
        assert name == "Призмы анимо: главный секрет (Мир Aniimo)"
        assert "youtu" not in name and VIDEO_ID not in name
        assert extractor.describe_calls == [URL], "describe зовётся по url джобы"
        # метаданные едут в джобе — заголовок разбора построит пайплайн
        assert job["display_title"] == "Призмы анимо: главный секрет"
        assert job["display_author"] == "Мир Aniimo"

    def test_describe_failure_falls_back_to_message_text(
        self, make_deps, make_message, make_extractor, make_router
    ):
        extractor = make_extractor(describe_error=RuntimeError("сеть лежит"))
        deps = make_deps(router=make_router([extractor]))
        msg = make_message(f"смотрите {URL} шедевр")

        asyncio.run(handle(msg, deps))

        job = asyncio.run(deps.queue.get())
        name = job["thread_name"]
        assert "смотрите" in name and "шедевр" in name
        assert job["display_title"] == "" and job["display_author"] == ""

    def test_describe_none_uses_message_text_too(
        self, make_deps, make_message, make_extractor, make_router
    ):
        # экстрактор без метаданных (статья) — имя из текста сообщения
        extractor = make_extractor(describe_result=None)
        deps = make_deps(router=make_router([extractor]))
        msg = make_message(f"почитай {URL} к вечернему")

        asyncio.run(handle(msg, deps))

        job = asyncio.run(deps.queue.get())
        assert "почитай" in job["thread_name"]


class _FailingPutJobStore:
    """Джоба-стор с падающим put: БД недоступна (P3-3). put не пишет строку,
    count_new делегируется обёрнутому стору — по нему видно, что записи нет."""

    def __init__(self, inner):
        self._inner = inner

    def put(self, payload):
        raise RuntimeError("БД недоступна")

    def count_new(self):
        return self._inner.count_new()


class TestJobStoreFailure:
    def test_jobs_put_failure_silent_without_raising(
        self, make_deps, make_message, make_thread, make_extractor, make_router, make_job_store
    ):
        # сбой deps.jobs.put не роняет on_message: джоба в очередь и БД не
        # попадает, пользователь ничего не получает (сообщений о сбоях больше нет)
        deps = make_deps(
            router=make_router([make_extractor()]),
            jobs=_FailingPutJobStore(make_job_store()),
        )
        msg = make_message(f"глянь {URL}")

        asyncio.run(handle(msg, deps))  # RuntimeError из put не вылетает

        assert msg.replies == [] and msg.status.edits == [], "бот молчит"
        assert deps.queue.empty(), "джоба без записи в БД в очередь не ставится"
        assert deps.jobs.count_new() == 0, "в БД ничего не записано"
