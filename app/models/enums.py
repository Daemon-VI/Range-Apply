"""Enumerations for career data models."""

from enum import Enum


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    INFERRED = "INFERRED"
    CONFLICT = "CONFLICT"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class SkillCategory(str, Enum):
    PROGRAMMING = "Programming"
    BACKEND = "Backend"
    FRONTEND = "Frontend"
    DATABASES = "Databases"
    SYSTEMS = "Systems"
    AI_ML = "AI/ML"
    DATA = "Data"
    DEVOPS = "DevOps"
    CLOUD = "Cloud"
    TOOLS = "Tools"
    OTHER = "Other"


class FactCategory(str, Enum):
    IDENTITY = "identity"
    EDUCATION = "education"
    SKILL = "skill"
    PROJECT = "project"
    EXPERIENCE = "experience"
    ACHIEVEMENT = "achievement"
    PREFERENCE = "preference"
    METRIC = "metric"
