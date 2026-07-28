import time


class SystemClock:
    def timestamp(self) -> int:
        return int(time.time())
