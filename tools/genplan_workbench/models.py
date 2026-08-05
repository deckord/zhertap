from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class PointRole(StrEnum):
    train = "train"
    checkpoint = "checkpoint"


class WorkflowStatus(StrEnum):
    proposed = "proposed"
    qa_pending = "qa_pending"


class TransformType(StrEnum):
    affine = "affine"
    projective = "projective"


class GCP(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    pixel_x: float = Field(ge=0)
    pixel_y: float = Field(ge=0)
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    role: PointRole = PointRole.train
    label: str = Field(default="", max_length=200)
    reference_source: str = Field(default="", max_length=200)


class WorkbenchSave(BaseModel):
    page: int = Field(default=1, ge=1, le=500)
    image_width_px: int = Field(gt=0, le=200_000)
    image_height_px: int = Field(gt=0, le=200_000)
    transform_type: TransformType = TransformType.affine
    workflow_status: WorkflowStatus = WorkflowStatus.proposed
    operator: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=5_000)
    points: list[GCP] = Field(max_length=500)

    @field_validator("points")
    @classmethod
    def unique_point_ids(cls, value: list[GCP]) -> list[GCP]:
        point_ids = [point.id for point in value]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("GCP IDs must be unique")
        return value

