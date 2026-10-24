import argparse
import asyncio
import logging
from uuid import uuid4

from pydantic import ValidationError

from collective.api.errors import DomainError
from collective.domain.clock import SystemClock
from collective.jobs.handlers import configured_handlers
from collective.repositories import jobs
from collective.repositories.db import Database
from collective.settings import Settings

logger = logging.getLogger(__name__)


class RetryableFailure(Exception):
    pass


class Runner:
    def __init__(self, database, *, clock=None, handlers=None, settings=None):
        self.database = database
        self.clock = clock or SystemClock()
        self.handlers = (
            configured_handlers(settings or Settings()) if handlers is None else handlers
        )
        self.worker = uuid4()

    async def run_once(self):
        async with self.database.transaction("coordinator", None) as connection:
            job = await jobs.claim(connection, self.worker, self.handlers, self.clock.now())
        if job is None:
            return False
        try:
            # A handler performs external preparation outside its own short commit transaction.
            await self.handlers[job["kind"]](self.database, job, self.clock)
        except jobs.LeaseLost:
            pass
        except Exception as exc:
            retryable = isinstance(exc, (RetryableFailure, jobs.NotDue))
            code = (
                "NOT_DUE"
                if isinstance(exc, jobs.NotDue)
                else (
                    "INVALID_INPUT"
                    if isinstance(exc, ValidationError)
                    else "RETRYABLE_FAILURE"
                    if retryable
                    else "JOB_FAILED"
                )
            )
            if isinstance(exc, DomainError):
                code = exc.code
            try:
                async with self.database.transaction("coordinator", None) as connection:
                    await jobs.fail(
                        connection,
                        job,
                        self.clock.now(),
                        retryable=retryable,
                        code=code,
                        retry_at=exc.due if isinstance(exc, jobs.NotDue) else None,
                    )
            except jobs.LeaseLost:
                pass
            logger.warning("Background job %s: %s (%s)", job["id"], code, type(exc).__name__)
        return True

    async def run(self):
        while True:
            try:
                if not await self.run_once():
                    await asyncio.sleep(1)
            except Exception as exc:
                logger.warning("Background runner unavailable (%s)", type(exc).__name__)
                await asyncio.sleep(5)


async def serve(once):
    settings = Settings()
    if settings.database_url is None:
        raise SystemExit("Configure DATABASE_URL before starting the worker.")
    database = Database(settings.database_url.get_secret_value())
    await database.open()
    try:
        runner = Runner(database, settings=settings)
        if once:
            print("Processed a due job." if await runner.run_once() else "No supported due job.")
        else:
            await runner.run()
    finally:
        await database.close()


def main():
    parser = argparse.ArgumentParser(description="Run Orbyte's durable background jobs.")
    parser.add_argument("--once", action="store_true")
    asyncio.run(serve(parser.parse_args().once))


if __name__ == "__main__":
    main()
