from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    app_timezone: str = "Asia/Shanghai"

    database_url: str
    redis_url: str

    jwt_secret: str

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "CSE Scheduler <noreply@example.com>"

    solver_time_limit: int = 120

    minimum_rest_hours: int = 11
    max_consecutive_work_days: int = 7
    max_consecutive_nights: int = 3
    night_forced_rest_days: int = 3

    # 【F3】员工未单独配置夜班策略时的缺省值
    night_default_max_consecutive: int = 3
    night_default_rest_days: int = 3
    # 【F3】夜班连班达标后的强制休息窗口内，允许出现哪些班次：
    #   默认 = 只能"休"或"假日夜班 Y"（Y 不计入连班，业务确认为独立班次；
    #   真实历史里也确实存在 3 连 N 之后接 Y 的情况）
    night_rest_allows_holiday_night: bool = True
    night_rest_allows_day: bool = False

    import_period_mode: str = "26_TO_25"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
