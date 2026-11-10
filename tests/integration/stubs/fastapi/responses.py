class JSONResponse:
    def __init__(self, content=None, status_code=200, headers=None):
        self.content, self.status_code, self.headers = content, status_code, headers
