"""Pydantic request/response models.

P1: Minimal — only models needed for request body validation.
P2: Will add auth-related models (RegisterRequest, LoginRequest, etc.)
"""
from pydantic import BaseModel
from typing import Optional


class StatusRequest(BaseModel):
    item_id: str
    action: str  # 'read', 'click', 'star', 'unstar', 'hide', 'unhide'


class FeedbackRequest(BaseModel):
    item_id: str
    type: str  # 'positive', 'irrelevant', 'low_quality', 'text', 'should_feature'
    topic: Optional[str] = None
    text: Optional[str] = None


class InterestCreate(BaseModel):
    name: str
    description: Optional[str] = None
    keywords: Optional[list] = None
    sort: Optional[str] = 'relevance'
    item_limit: Optional[int] = 50
    scope: Optional[str] = 'all'


class ActionCreate(BaseModel):
    title: str
    prompt: Optional[str] = None
    action_type: Optional[str] = None
    priority: Optional[str] = 'medium'
    reason: Optional[str] = None
    source_item_ids: Optional[list] = None
    direction: Optional[str] = None
    direction_label: Optional[str] = None
    source_type: Optional[str] = 'manual'
    related_project: Optional[str] = None
