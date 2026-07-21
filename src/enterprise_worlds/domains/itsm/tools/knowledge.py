"""Knowledge tools (3) — faithful port of the ITSM MCP's knowledge category.

Covers knowledge-article create/retrieve/update.
"""

from __future__ import annotations

from typing import List, Optional

from enterprise_worlds.domains.itsm import enums
from enterprise_worlds.domains.itsm.data_model import Knowledge
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool


class KnowledgeToolsMixin(ItsmToolsBase):
    """Knowledge-base article management tools."""

    # ------------------------------------------------------------------ helpers
    def _validate_knowledge_enums(self, *, state=None, visibility=None) -> None:
        """Reject out-of-set enum values, mirroring the reference's request-body enum gate."""
        self._check_enum("state", state, enums.KNOWLEDGE_STATE)
        self._check_enum("visibility", visibility, enums.KNOWLEDGE_VISIBILITY)

    @staticmethod
    def _kb_number_seq(collection: dict) -> int:
        """Next sequential integer for ``KB<NNNNNNN>`` numbers (max numeric + 1; 1 if empty)."""
        seqs = []
        for rec in collection.values():
            num = getattr(rec, "kb_number", None)
            if num and num.startswith("KB") and num[2:].isdigit():
                seqs.append(int(num[2:]))
        return (max(seqs) + 1) if seqs else 1

    def _require_owner(self, owner_id: Optional[str]) -> None:
        """Owner-FK existence check using the reference's ``USER_NOT_FOUND`` code.

        The shared ``_require_user`` helper raises the right message/field but with the base
        ``VALIDATION_ERROR`` code; the live knowledge endpoints surface owner-not-found as
        ``USER_NOT_FOUND`` (confirmed live on both create and update). Defined locally to avoid
        editing the shared base.
        """
        if owner_id is not None and owner_id not in self.db.users:
            raise ItsmError(
                f"User with ID '{owner_id}' not found",
                code="USER_NOT_FOUND",
                field="owner_id",
            )

    def _check_duplicate_article(
        self, title: str, owner_id: str, org_id: str, exclude_id: Optional[str] = None
    ) -> None:
        """Reject a (title, owner) collision within the org (mirrors the reference DUPLICATE_ARTICLE)."""
        for kid, art in self.db.knowledge.items():
            if kid == exclude_id:
                continue
            if art.org_id == org_id and art.title == title and art.owner_id == owner_id:
                raise ItsmError(
                    "A knowledge article with the same title and owner already exists",
                    code="DUPLICATE_ARTICLE",
                    field="title",
                )

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def create_knowledge_article(
        self,
        title: str,
        owner_id: str,
        body: Optional[str] = None,
        state: Optional[str] = None,
        visibility: Optional[str] = None,
    ) -> Knowledge:
        """Create a new knowledge article.

        The system auto-generates ``knowledge_id`` (KB_001, KB_002, ...) and ``kb_number``
        (KB0000001, KB0000002, ...) and timestamps. The article is scoped to the acting user's
        organization.

        Args:
            title: Article title (required, 1-120 characters).
            owner_id: User id who owns the article (author/responsible person, required).
            body: Full article content (detailed explanation, solutions, troubleshooting steps).
            state: Article state (draft, review, published, retired); defaults to 'published'.
            visibility: Article visibility (internal, external); defaults to 'internal'.

        Returns:
            The created knowledge article.
        """
        # Enum validation first (the reference validates the request body before FK checks).
        self._validate_knowledge_enums(state=state, visibility=visibility)
        # The create schema rejects an explicit empty ``body`` (distinct from the update path, which
        # treats body='' as a clear-to-NULL); the reference's validator message is verbatim below.
        if body == "":
            raise ItsmError(
                "Field cannot be empty string", code="VALIDATION_ERROR", field="body"
            )
        self._require_owner(owner_id)
        self._check_duplicate_article(title, owner_id, self._acting_org())

        knowledge_id, _ = self._make_id(self.db.knowledge, "KB")
        kb_seq = self._kb_number_seq(self.db.knowledge)
        now = self._now()
        article = Knowledge(
            knowledge_id=knowledge_id,
            kb_number=f"KB{kb_seq:07d}",
            title=title,
            short_description=None,  # tools never set short_description (only the seed bakes it)
            body=body,
            state=state or "published",
            visibility=visibility or "internal",
            owner_id=owner_id,
            org_id=self._acting_org(),
            created_on=now,
            updated_on=now,
        )
        self.db.knowledge[knowledge_id] = article
        return article

    @is_tool(ToolType.WRITE)
    def update_knowledge_article(
        self,
        knowledge_id: Optional[str] = None,
        kb_number: Optional[str] = None,
        title: Optional[str] = None,
        body: Optional[str] = None,
        state: Optional[str] = None,
        visibility: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> Knowledge:
        """Update a knowledge article by its ``knowledge_id``. Only passed fields are changed.

        The article is located by ``knowledge_id``. ``kb_number`` is accepted for compatibility
        but the underlying server requires ``knowledge_id`` to identify the record.

        Args:
            knowledge_id: Unique identifier of the article to update (e.g., KB_001).
            kb_number: KB number of the article (e.g., KB0000001), mutually exclusive with knowledge_id; the server routes on knowledge_id, so knowledge_id must be provided.
            title: Updated title (1-120 characters).
            body: Updated body content.
            state: Updated state (draft, review, published, retired).
            visibility: Updated visibility (internal, external).
            owner_id: Updated owner user id.

        Returns:
            The updated knowledge article.
        """
        # Enum validation first (the reference validates the request body before FK/existence checks).
        self._validate_knowledge_enums(state=state, visibility=visibility)
        if knowledge_id is None:
            # The server requires knowledge_id as a path parameter (kb_number alone is rejected).
            raise ItsmError("Field required", code="VALIDATION_ERROR", field="knowledge_id")

        updates = {
            "title": title, "body": body, "state": state,
            "visibility": visibility, "owner_id": owner_id,
        }
        # Empty-string handling mirrors the reference's knowledge "clear" model: an explicit '' is
        # normalized to None and the (nullable) column is cleared; ''-as-None for the NOT NULL
        # ``title`` violates the column constraint (VALIDATION_ERROR). A cleared owner_id is not
        # FK-checked (clearing a link never triggers existence validation).
        provided = {k: v for k, v in updates.items() if v is not None}
        for k in list(provided):
            if provided[k] == "":
                if k == "title":
                    raise ItsmError("title cannot be empty", code="VALIDATION_ERROR", field="title")
                provided[k] = None
        if not provided:
            raise ItsmError(
                "At least one field must be provided for update besides the identifier",
                code="VALIDATION_ERROR",
            )

        article = self.db.knowledge.get(knowledge_id)
        if article is None:
            raise ItsmError(
                f"Knowledge article not found with identifier '{knowledge_id}'",
                code="NOT_FOUND",
                field=None,
            )

        self._require_owner(provided.get("owner_id"))

        # Reject a (title, owner) collision with a DIFFERENT article in the org.
        if "title" in provided or "owner_id" in provided:
            self._check_duplicate_article(
                provided["title"] if "title" in provided else article.title,
                provided["owner_id"] if "owner_id" in provided else article.owner_id,
                article.org_id,
                exclude_id=knowledge_id,
            )

        # A no-op update (every provided field already equal) is rejected, not silently re-stamped.
        changed = {k: v for k, v in provided.items() if getattr(article, k) != v}
        if not changed:
            raise ItsmError("No changes detected", code="NO_CHANGES_DETECTED")
        for field, value in changed.items():
            setattr(article, field, value)
        article.updated_on = self._now()
        return article

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def retrieve_knowledge_articles(
        self,
        knowledge_id: Optional[str] = None,
        kb_number: Optional[str] = None,
        title: Optional[str] = None,
        body: Optional[str] = None,
        state: Optional[str] = None,
        visibility: Optional[str] = None,
        owner_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> dict:
        """List knowledge articles with optional filters (all ANDed). No filters returns all.

        Articles are returned across all organizations, sorted by creation time (newest first).

        Args:
            knowledge_id: Filter by knowledge id (exact match, e.g., KB_001).
            kb_number: Filter by KB number (exact match, e.g., KB0000001).
            title: Filter by title (partial match, case-insensitive).
            body: Filter by body content (partial match, case-insensitive).
            state: Filter by article state (draft, review, published, retired).
            visibility: Filter by visibility level (internal, external).
            owner_id: Filter by owner user id (exact match).
            created_after: Only articles created strictly after this ISO 8601 timestamp.
            created_before: Only articles created on or before this ISO 8601 timestamp.

        Returns:
            A mapping with 'knowledges' (the matching articles) and 'total_count'.
        """
        # The reference validates enum-typed filters too (invalid value -> error, not empty result).
        self._validate_knowledge_enums(state=state, visibility=visibility)
        out: List[Knowledge] = []
        for art in self.db.knowledge.values():
            if knowledge_id is not None and art.knowledge_id != knowledge_id:
                continue
            if kb_number is not None and art.kb_number != kb_number:
                continue
            if title is not None and title.lower() not in (art.title or "").lower():
                continue
            if body is not None and body.lower() not in (art.body or "").lower():
                continue
            if state is not None and art.state != state:
                continue
            if visibility is not None and art.visibility != visibility:
                continue
            if owner_id is not None and art.owner_id != owner_id:
                continue
            if created_after is not None and not ((art.created_on or "") > created_after):
                continue
            if created_before is not None and not ((art.created_on or "") <= created_before):
                continue
            out.append(art)

        out.sort(key=lambda a: (a.created_on or ""), reverse=True)
        return {"knowledges": out, "total_count": len(out)}
