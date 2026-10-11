"""E2E-тесты handle() фазы 4: Router, дедуп в БД, джоба нового формата, имя треда.

Контракт (docs/ARCHITECTURE.md §6, §10):
- route = deps.router.route(message.content): совпадение ищут экстракторы
  (find -> границы); None — в сообщении нет ссылки, никаких действий;
- дедуп: deps.dedup.mark(str(message.id), extractor.canonical_id(route.url)) —
  False (уже обработано) -> выход. Дедуп теперь в БД (in-memory set удалён):
  переживает рестарт, ключ — сообщение, а не canonical (та же ссылка в другом
  сообщении разбирается);
- джоба нового формата: kind == extractor.name, url — совпавшая подстрока,
  canonical_id от экстрактора, thread_id/message_id/status_message_id/status +
  job_id (вернул deps.jobs.put). В БД payload уходит БЕЗ status-объекта
  (он не сериализуется в JSON) — статус прикрепляется только к джобе в очереди
  и восстанавливается после рестарта через fetch_message;
- имя треда: текст сообщения без ссылки (срез route.start:route.end); если
  после обрезки пусто — f"Разбор {route.url}"[:90].
"""

import asyncio
from types import SimpleNamespace

import discord

from bot.discord_app import handle

VIDEO_ID = "abcdefghijk"
URL = f"https://youtu.be/{VIDEO_ID}"


class ThreadAlreadyExistsError(discord.HTTPException):
    """Фейк ошибки Discord 160004 «тред под этим сообщением уже существует»."""

    def __init__(self):
        self.response = SimpleNamespace(status=400, reason="x")
        self.status = 400
        self.code = 160004
        self.text = "тред уже существует"


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
        assert job["thread_id"] == 555
        assert job["message_id"] == msg.id
        assert job["status"] is msg.status
        assert job["status_message_id"] == 999
        assert isinstance(job["job_id"], int)
        assert deps.queue.empty()

    def test_repeat_of_same_message_deduplicated(
        self, make_deps, make_message, make_thread, make_extractor, make_router
    ):
        extractor = make_extractor()
        thread = make_thread(555)
        deps = make_deps(router=make_router([extractor]), thread=thread)
        msg = make_message(f"глянь {URL}", thread=thread)

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
            msg = make_message(f"глянь {URL}", id=3003, thread=make_thread(555))
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
        assert msg.status.edits == []

    def test_job_payload_in_store_excludes_status_object(
        self, make_deps, make_message, make_thread, make_extractor, make_router
    ):
        # status-объект не сериализуется в JSON: в БД уходит payload без него,
        # статус восстанавливается отдельно (fetch_message по status_message_id)
        extractor = make_extractor()
        deps = make_deps(router=make_router([extractor]), thread=make_thread(555))
        msg = make_message(f"глянь {URL}", status_id=999, thread=make_thread(555))

        asyncio.run(handle(msg, deps))

        queued = asyncio.run(deps.queue.get())
        rows = deps.jobs.take_new()
        assert len(rows) == 1
        stored = rows[0]
        assert stored["job_id"] == queued["job_id"], "джоба в очереди знает свой id в БД"
        assert "status" not in stored, "status-объект в БД не пишется"
        assert stored["status_message_id"] == 999
        assert stored["url"] == URL
        assert stored["canonical_id"] == extractor.canonical_id(URL)


class TestThreadLifecycle:
    def test_thread_already_exists_160004_reused(self, make_deps, make_message, make_thread, make_extractor, make_router):
        # тред создан до рестарта бота: Discord вернёт 160004 — переиспользуем его
        deps = make_deps(router=make_router([make_extractor()]))
        existing = make_thread(2002)
        channel = SimpleNamespace(id=10, threads=[existing])
        msg = make_message(
            f"ссылка {URL}",
            id=2002,
            channel=channel,
            thread=ThreadAlreadyExistsError(),
        )

        asyncio.run(handle(msg, deps))

        job = asyncio.run(deps.queue.get())
        assert job["thread_id"] == existing.id == msg.id
        assert deps.queue.empty()  # одна задача

    def test_no_thread_permission_no_job_and_warning(self, make_deps, make_message, make_extractor, make_router):
        deps = make_deps(router=make_router([make_extractor()]))
        forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "нет прав")
        msg = make_message(f"ссылка {URL}", thread=forbidden)

        asyncio.run(handle(msg, deps))

        assert deps.queue.empty()
        assert deps.jobs.count_new() == 0
        assert msg.status.edits, "пользователю не сообщили о проблеме"

    def test_queue_position_displayed_when_busy(self, make_deps, make_message, make_thread, make_extractor, make_router):
        deps = make_deps(router=make_router([make_extractor()]))
        asyncio.run(
            deps.queue.put(
                {
                    "kind": "video",
                    "url": "https://youtu.be/aaaaaaaaaaa",
                    "canonical_id": "youtube:video:aaaaaaaaaaa",
                    "thread_id": 1,
                    "message_id": 2,
                }
            )
        )
        msg = make_message(URL, thread=make_thread(555))

        asyncio.run(handle(msg, deps))

        # позиция в очереди показана в статусе (наш job второй)
        assert any(content and "позиция" in content and "2" in content for content in msg.status.edits)


class TestThreadName:
    def test_strips_link_keeps_text(self, make_deps, make_message, make_thread, make_extractor, make_router):
        deps = make_deps(router=make_router([make_extractor()]))
        msg = make_message(f"смотрите {URL} это интересно", thread=make_thread(555))

        asyncio.run(handle(msg, deps))

        name = msg.thread_kwargs["name"]
        assert "youtu" not in name and VIDEO_ID not in name
        assert "смотрите" in name and "это интересно" in name

    def test_fallback_when_only_link(self, make_deps, make_message, make_thread, make_extractor, make_router):
        deps = make_deps(router=make_router([make_extractor()]))
        msg = make_message(URL, thread=make_thread(555))

        asyncio.run(handle(msg, deps))

        assert msg.thread_kwargs["name"] == f"Разбор {URL}"[:90]

    def test_truncated_to_90(self, make_deps, make_message, make_thread, make_extractor, make_router):
        deps = make_deps(router=make_router([make_extractor()]))
        msg = make_message("т" * 300 + " " + URL, thread=make_thread(555))

        asyncio.run(handle(msg, deps))

        assert len(msg.thread_kwargs["name"]) <= 90

    def test_describe_names_thread_without_link(
        self, make_deps, make_message, make_thread, make_extractor, make_router
    ):
        # UX-итерация: имя треда = «Название (Автор)», ссылок в имени нет
        extractor = make_extractor(describe_result=("Призмы анимо: главный секрет", "Мир Aniimo"))
        deps = make_deps(router=make_router([extractor]))
        msg = make_message(URL, thread=make_thread(555))

        asyncio.run(handle(msg, deps))

        name = msg.thread_kwargs["name"]
        assert name == "Призмы анимо: главный секрет (Мир Aniimo)"
        assert "youtu" not in name and VIDEO_ID not in name
        assert extractor.describe_calls == [URL], "describe зовётся по url джобы"
        # метаданные едут в джобе — заголовок разбора построит пайплайн
        job = asyncio.run(deps.queue.get())
        assert job["display_title"] == "Призмы анимо: главный секрет"
        assert job["display_author"] == "Мир Aniimo"

    def test_describe_failure_falls_back_to_message_text(
        self, make_deps, make_message, make_thread, make_extractor, make_router
    ):
        extractor = make_extractor(describe_error=RuntimeError("сеть лежит"))
        deps = make_deps(router=make_router([extractor]))
        msg = make_message(f"смотрите {URL} шедевр", thread=make_thread(555))

        asyncio.run(handle(msg, deps))

        name = msg.thread_kwargs["name"]
        assert "смотрите" in name and "шедевр" in name
        job = asyncio.run(deps.queue.get())
        assert job["display_title"] == "" and job["display_author"] == ""

    def test_describe_none_uses_message_text_too(
        self, make_deps, make_message, make_thread, make_extractor, make_router
    ):
        # экстрактор без метаданных (статья) — старое поведение по тексту сообщения
        extractor = make_extractor(describe_result=None)
        deps = make_deps(router=make_router([extractor]))
        msg = make_message(f"почитай {URL} к вечернему", thread=make_thread(555))

        asyncio.run(handle(msg, deps))

        assert "почитай" in msg.thread_kwargs["name"]


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
    def test_jobs_put_failure_edits_status_without_raising(
        self, make_deps, make_message, make_thread, make_extractor, make_router, make_job_store
    ):
        # сбой deps.jobs.put не теряет сообщение молча: пользователь получает
        # предупреждение в статусе, джоба в очередь и БД не попадает, исключение
        # наружу не идёт (on_message не увидит падения и не залогирует его)
        deps = make_deps(
            router=make_router([make_extractor()]),
            jobs=_FailingPutJobStore(make_job_store()),
        )
        msg = make_message(f"глянь {URL}", thread=make_thread(555))

        asyncio.run(handle(msg, deps))  # RuntimeError из put не вылетает

        assert any(
            c and "Не удалось поставить разбор в очередь" in c for c in msg.status.edits
        ), "пользователю сообщено о сбое очереди"
        assert deps.queue.empty(), "джоба без записи в БД в очередь не ставится"
        assert deps.jobs.count_new() == 0, "в БД ничего не записано"
