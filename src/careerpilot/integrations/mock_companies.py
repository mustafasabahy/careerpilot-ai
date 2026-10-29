"""Local mock company source; returned website is metadata, never fetched."""

from careerpilot.application.discovery_contracts import CompanyInfo, ResourceNotFound


class MockCompanyAdapter:
    def __init__(self, companies: tuple[CompanyInfo, ...]) -> None:
        self._companies = {company.company_id: company for company in companies}

    def get(self, identifier: str, *, timeout_seconds: float) -> CompanyInfo:
        if timeout_seconds <= 0:
            raise TimeoutError()
        if identifier not in self._companies:
            raise ResourceNotFound()
        return self._companies[identifier]
