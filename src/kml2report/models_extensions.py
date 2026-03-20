"""Extended data models for geometry analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from kml2report.models import Feature, Geometry


@dataclass
class GeometryAnalysisResult:
    """Results from geometry analysis of KML/KMZ file."""
    features: List[Feature] = field(default_factory=list)
    summary: Optional[Dict] = None
    linear_runs: List[Dict] = field(default_factory=list)
    validation_results: List[Dict] = field(default_factory=list)
    total_length: float = 0.0
    segment_count: int = 0
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'total_features': len(self.features),
            'total_length': self.total_length,
            'segment_count': self.segment_count,
            'summary': self.summary or {},
            'linear_runs': self.linear_runs,
            'validation_results': self.validation_results
        }


@dataclass
class DuctSegment:
    """Individual duct segment with metadata."""
    feature_id: str
    duct_size: str
    length: float
    start_coord: Tuple[float, float]
    end_coord: Tuple[float, float]
    layer: str = ""
    attributes: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for CSV/JSON export."""
        return {
            'feature_id': self.feature_id,
            'duct_size': self.duct_size,
            'length_ft': self.length,
            'start_lon': self.start_coord[0],
            'start_lat': self.start_coord[1],
            'end_lon': self.end_coord[0],
            'end_lat': self.end_coord[1],
            'layer': self.layer,
            **self.attributes
        }


@dataclass
class ConnectedRun:
    """A connected run of duct segments."""
    run_id: str
    segments: List[DuctSegment] = field(default_factory=list)
    total_length: float = 0.0
    start_coord: Optional[Tuple[float, float]] = None
    end_coord: Optional[Tuple[float, float]] = None
    duct_sizes: List[str] = field(default_factory=list)
    
    def add_segment(self, segment: DuctSegment) -> None:
        """Add a segment to this run."""
        self.segments.append(segment)
        self.total_length += segment.length
        
        if segment.duct_size not in self.duct_sizes:
            self.duct_sizes.append(segment.duct_size)
        
        # Update start/end coordinates
        if not self.start_coord:
            self.start_coord = segment.start_coord
        self.end_coord = segment.end_coord
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for export."""
        return {
            'run_id': self.run_id,
            'total_length_ft': self.total_length,
            'segment_count': len(self.segments),
            'start_lon': self.start_coord[0] if self.start_coord else None,
            'start_lat': self.start_coord[1] if self.start_coord else None,
            'end_lon': self.end_coord[0] if self.end_coord else None,
            'end_lat': self.end_coord[1] if self.end_coord else None,
            'duct_sizes': ', '.join(self.duct_sizes),
            'segments': [s.to_dict() for s in self.segments]
        }
