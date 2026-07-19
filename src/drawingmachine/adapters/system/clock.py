from datetime import datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().astimezone().replace(microsecond=0)
