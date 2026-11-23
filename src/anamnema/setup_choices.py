"""Nonsecret operator choices shared by the wizard and manual configuration."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Account = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_-]*$")]


class ServiceAccounts(BaseModel):
    """Requested accounts; None leaves a system database's account to its package."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    application: Account | None = None
    models: Account | None = None
    archive: Account | None = None
    graph: Account | None = None

    @field_validator("*")
    @classmethod
    def not_root(cls, value: str | None) -> str | None:
        """Do not plan a memory service or model under the root account."""
        if value == "root":
            raise ValueError("choose a non-root service account")
        return value


class SetupChoices(BaseModel):
    """Selections are intentions, not verified service or keyring capabilities."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    run_when_logged_out: bool | None = None
    service_scope: Literal["user", "system"] | None = None
    accounts: ServiceAccounts = Field(default_factory=ServiceAccounts)
    keyring_setup: Literal["manual", "guided"] | None = None
    keyring_owner: Account | None = None

    @model_validator(mode="after")
    def coherent_accounts(self) -> Self:
        """Reject contradictory user-service or keyring account requests."""
        account = self.accounts.application
        if self.keyring_owner == "root":
            raise ValueError("choose a non-root keyring owner")
        if self.keyring_owner and account and self.keyring_owner != account:
            raise ValueError("the application must use its own account's keyring")
        if self.service_scope == "user":
            if account is None:
                raise ValueError("user application services must name their account")
        return self
