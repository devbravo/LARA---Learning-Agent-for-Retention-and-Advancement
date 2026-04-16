from pydantic import BaseModel, ConfigDict


class TelegramChat(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int


class TelegramMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message_id: int
    chat: TelegramChat
    text: str | None = None


class TelegramCallbackQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    data: str | None = None
    message: TelegramMessage | None = None


class TelegramUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    update_id: int
    message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None
