import os

from pydantic import BaseModel


def SettingsConfigDict(**k):
    return {}


class BaseSettings(BaseModel):
    def __init__(self, **values):
        for name in type(self).model_fields:
            if name not in values and name in os.environ:
                values[name] = os.environ[name]
        super().__init__(**values)
