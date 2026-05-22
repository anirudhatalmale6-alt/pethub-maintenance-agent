from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    AGENT_NAME: str = "maintenance"
    MANAGER_URL: str = "http://127.0.0.1:8100"
    API_PORT: int = 8104
    WP_URL: str = "https://pethubonline.com"
    WP_USER: str = "jasonsarah2026"
    WP_APP_PASSWORD: str = "EIul 3KqI 3fY7 yLbk Ltva aPnj"
    OPENAI_API_KEY: str = ""
    HEARTBEAT_INTERVAL: int = 120
    DB_PATH: str = "/var/lib/freelancer/projects/40416335/maintenance-agent/data/maintenance_data.json"

    class Config:
        env_file = ".env"


settings = Settings()
