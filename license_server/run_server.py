import uvicorn

from license_server.config import get_license_server_settings

if __name__ == "__main__":
    settings = get_license_server_settings()
    uvicorn.run("license_server.main:app", host=settings.host, port=settings.port, reload=False)
