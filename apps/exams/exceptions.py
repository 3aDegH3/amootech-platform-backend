class Quiz24Error(Exception):
    def __init__(self, status, message, *, http_status=None, safe_response=None, api_status=502):
        self.status = status
        self.message = message
        self.http_status = http_status
        self.safe_response = safe_response
        self.api_status = api_status
        super().__init__(message)


def quiz24_error_data(error):
    detail = error.message
    if error.status is not None:
        detail = f"{detail} (کد Quiz24: {error.status})"
    return {"detail": detail, "provider_code": error.status, "provider_message": error.message}
