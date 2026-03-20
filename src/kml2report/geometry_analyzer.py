"""Geometry analysis module for KML/KMZ files.

Analyzes linear geometries (ducts, cables) and provides project-wide
aggregation of lengths by type.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from bs4 import BeautifulSoup

try:
    import pyproj
except ImportError:
    pyproj = None

from kml2report.models import Feature, Geometry


@dataclass
class DuctInfo:
    """Information about a duct segment."""
    duct_size: str
    length: float
    geometry: Optional[Geometry] = None
    feature_id: str = ""


@dataclass
class LinearRun:
    """A connected run of linear features."""
    features: List[Feature] = field(default_factory=list)
    total_length: float = 0.0
    start_coord: Optional[Tuple[float, float]] = None
    end_coord: Optional[Tuple[float, float]] = None
    duct_sizes: List[str] = field(default_factory=list)
    
    def add_feature(self, feature: Feature) -> None:
        """Add a feature to this run."""
        self.features.append(feature)
        self.duct_sizes.append(feature.name)
        
        # Update start/end coordinates
        if feature.geometry and feature.geometry.coordinates:
            if not self.start_coord:
                self.start_coord = (
                    feature.geometry.coordinates[0][0],
                    feature.geometry.coordinates[0][1]
                )
            self.end_coord = (
                feature.geometry.coordinates[-1][0],
                feature.geometry.coordinates[-1][1]
            )


@dataclass
class ProjectSummary:
    """Summary of all duct lengths in a project."""
    single_duct_lengths: Dict[str, float] = field(default_factory=dict)
    multi_duct_lengths: Dict[str, float] = field(default_factory=dict)
    aerial_lengths: Dict[str, float] = field(default_factory=dict)
    other_lengths: Dict[str, float] = field(default_factory=dict)
    total_length: float = 0.0
    segment_count: int = 0
    
    def add_single_duct(self, duct_size: str, length: float) -> None:
        """Add length to single duct category."""
        if duct_size not in self.single_duct_lengths:
            self.single_duct_lengths[duct_size] = 0.0
        self.single_duct_lengths[duct_size] += length
        self.total_length += length
        self.segment_count += 1
    
    def add_multi_duct(self, config: str, length: float) -> None:
        """Add length to multi-duct category."""
        if config not in self.multi_duct_lengths:
            self.multi_duct_lengths[config] = 0.0
        self.multi_duct_lengths[config] += length
        self.total_length += length
        self.segment_count += 1
    
    def add_aerial(self, wire_type: str, length: float) -> None:
        """Add length to aerial category."""
        if wire_type not in self.aerial_lengths:
            self.aerial_lengths[wire_type] = 0.0
        self.aerial_lengths[wire_type] += length
        self.total_length += length
        self.segment_count += 1
    
    def add_other(self, category: str, length: float) -> None:
        """Add length to other category."""
        if category not in self.other_lengths:
            self.other_lengths[category] = 0.0
        self.other_lengths[category] += length
        self.total_length += length
        self.segment_count += 1


def parse_description_html(description: str) -> Dict[str, str]:
    """Extract attributes from HTML description table.
    
    Args:
        description: HTML content from KML description field
        
    Returns:
        Dictionary of attribute key-value pairs
    """
    if not description:
        return {}
    
    # Parse HTML
    soup = BeautifulSoup(description, 'html.parser')
    
    # Find all table rows with key-value pairs
    attributes = {}
    rows = soup.find_all('tr')
    
    for row in rows:
        cells = row.find_all('td')
        if len(cells) >= 2:
            key = cells[0].get_text(strip=True)
            value = cells[1].get_text(strip=True)
            attributes[key] = value
    
    return attributes


def extract_duct_info(feature: Feature) -> DuctInfo:
    """Extract duct information from a feature.
    
    Args:
        feature: KML feature object
        
    Returns:
        DuctInfo object with duct size and length
    """
    # Get duct size from name
    duct_size = feature.name or "Unknown"
    
    # Parse HTML description for length
    attrs = parse_description_html(feature.raw_description)
    length_str = attrs.get('New_Length', '0')
    
    # Clean and convert length
    try:
        # Remove commas and convert to float
        length = float(re.sub(r'[^\d.]', '', str(length_str)))
    except (ValueError, TypeError):
        length = 0.0
    
    return DuctInfo(
        duct_size=duct_size,
        length=length,
        geometry=feature.geometry,
        feature_id=feature.placemark_id
    )


def calculate_linestring_length(geometry: Geometry, unit: str = 'feet') -> float:
    """Calculate accurate length of LineString geometry.
    
    Args:
        geometry: Geometry object
        unit: Output unit ('feet' or 'meters')
        
    Returns:
        Length in specified units
    """
    if not geometry or geometry.geometry_type != 'LineString':
        return 0.0
    
    if not geometry.coordinates or len(geometry.coordinates) < 2:
        return 0.0
    
    # Use pyproj if available for accurate geodesic calculations
    if pyproj:
        geod = pyproj.Geod(ellps='WGS84')
        total_length = 0.0
        
        for i in range(len(geometry.coordinates) - 1):
            lon1, lat1 = geometry.coordinates[i][:2]
            lon2, lat2 = geometry.coordinates[i + 1][:2]
            _, _, segment_length = geod.inv(lon1, lat1, lon2, lat2)
            total_length += segment_length
        
        # Convert to feet if needed
        if unit == 'feet':
            total_length *= 3.28084
        
        return total_length
    else:
        # Fallback to simple haversine approximation
        total_length = 0.0
        
        for i in range(len(geometry.coordinates) - 1):
            lon1, lat1 = geometry.coordinates[i][:2]
            lon2, lat2 = geometry.coordinates[i + 1][:2]
            
            # Convert to radians
            lat1, lon1 = math.radians(lat1), math.radians(lon1)
            lat2, lon2 = math.radians(lat2), math.radians(lon2)
            
            # Haversine formula
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            a = (math.sin(dlat/2)**2 + 
                 math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2)
            c = 2 * math.asin(math.sqrt(a))
            
            # Earth radius in meters
            R = 6371000
            segment_length = R * c
            
            # Convert to feet if needed
            if unit == 'feet':
                segment_length *= 3.28084
            
            total_length += segment_length
        
        return total_length


def aggregate_project_footage(features: List[Feature]) -> ProjectSummary:
    """Aggregate total footage by duct run type for entire project.
    
    Args:
        features: List of KML features
        
    Returns:
        ProjectSummary with aggregated lengths
    """
    summary = ProjectSummary()
    
    for feature in features:
        if not feature.geometry or feature.geometry.geometry_type != 'LineString':
            continue
            
        # Extract duct info from feature
        duct_info = extract_duct_info(feature)
        
        # Based on sample KMZ, all appear to be single duct runs
        # The duct size is in the name field
        duct_size = duct_info.duct_size
        length = duct_info.length
        
        # Add to appropriate category
        if duct_size in ['2"', '3"', '4"', '2 inch', '3 inch', '4 inch']:
            # Standardize duct size format
            standardized = duct_size.replace(' inch', '"')
            summary.add_single_duct(standardized, length)
        elif 'messenger' in duct_size.lower() or 'aerial' in duct_size.lower():
            summary.add_aerial(duct_size, length)
        else:
            # Handle other sizes or categories
            summary.add_other(duct_size, length)
    
    return summary


def validate_lengths(features: List[Feature]) -> List[Dict[str, float]]:
    """Validate pre-calculated lengths against coordinate calculations.
    
    Args:
        features: List of KML features
        
    Returns:
        List of validation results with differences
    """
    results = []
    
    for feature in features:
        if not feature.geometry or feature.geometry.geometry_type != 'LineString':
            continue
            
        # Get pre-calculated length from description
        duct_info = extract_duct_info(feature)
        pre_calc_length = duct_info.length
        
        # Calculate from coordinates
        calc_length = calculate_linestring_length(feature.geometry)
        
        # Calculate difference
        diff = abs(pre_calc_length - calc_length)
        diff_pct = (diff / pre_calc_length * 100) if pre_calc_length > 0 else 0
        
        results.append({
            'feature_id': feature.placemark_id,
            'duct_size': duct_info.duct_size,
            'pre_calculated': pre_calc_length,
            'calculated': calc_length,
            'difference': diff,
            'difference_pct': diff_pct
        })
    
    return results


def detect_linear_runs(features: List[Feature], 
                      proximity_threshold: float = 0.00001) -> List[LinearRun]:
    """Detect connected linear runs from LineString features.
    
    Args:
        features: List of KML features
        proximity_threshold: Distance threshold in degrees (~1 meter)
        
    Returns:
        List of connected linear runs
    """
    runs = []
    visited = set()
    
    for feature in features:
        if (feature.placemark_id in visited or 
            not feature.geometry or 
            feature.geometry.geometry_type != 'LineString'):
            continue
        
        run = LinearRun()
        run.add_feature(feature)
        visited.add(feature.placemark_id)
        
        # Find connected features by proximity
        while True:
            connected = find_connected_features_by_proximity(
                run, features, visited, proximity_threshold
            )
            if not connected:
                break
            for cf in connected:
                run.add_feature(cf)
                visited.add(cf.placemark_id)
        
        runs.append(run)
    
    return runs


def find_connected_features_by_proximity(
    run: LinearRun, 
    features: List[Feature], 
    visited: set,
    threshold: float
) -> List[Feature]:
    """Find features connected to the current run.
    
    Args:
        run: Current linear run
        features: All features to search
        visited: Set of already visited feature IDs
        threshold: Proximity threshold in degrees
        
    Returns:
        List of connected features
    """
    connected = []
    
    if not run.end_coord:
        return connected
    
    for feature in features:
        if (feature.placemark_id in visited or
            not feature.geometry or
            feature.geometry.geometry_type != 'LineString'):
            continue
        
        # Check if this feature connects to the run
        if feature.geometry.coordinates:
            # Check start point
            start = feature.geometry.coordinates[0][:2]
            end = feature.geometry.coordinates[-1][:2]
            
            # Check proximity to run end
            dist_to_start = math.sqrt(
                (start[0] - run.end_coord[0])**2 + 
                (start[1] - run.end_coord[1])**2
            )
            dist_to_end = math.sqrt(
                (end[0] - run.end_coord[0])**2 + 
                (end[1] - run.end_coord[1])**2
            )
            
            if dist_to_start < threshold or dist_to_end < threshold:
                connected.append(feature)
    
    return connected


def generate_summary_report(summary: ProjectSummary) -> str:
    """Generate a text summary of the project analysis.
    
    Args:
        summary: ProjectSummary object
        
    Returns:
        Formatted summary report
    """
    lines = []
    lines.append("Project Summary - Total Footage:")
    lines.append("=" * 40)
    lines.append("")
    
    # Single duct runs
    if summary.single_duct_lengths:
        lines.append("Single Duct Runs:")
        lines.append("-" * 20)
        for size, length in sorted(summary.single_duct_lengths.items()):
            lines.append(f"- {size} duct: {length:,.2f} ft")
        total_single = sum(summary.single_duct_lengths.values())
        lines.append(f"Total Single Duct: {total_single:,.2f} ft")
        lines.append("")
    
    # Multi-duct runs
    if summary.multi_duct_lengths:
        lines.append("Multi-Duct Packages:")
        lines.append("-" * 20)
        for config, length in sorted(summary.multi_duct_lengths.items()):
            lines.append(f"- {config}: {length:,.2f} ft")
        lines.append("")
    
    # Aerial runs
    if summary.aerial_lengths:
        lines.append("Aerial Construction:")
        lines.append("-" * 20)
        for wire_type, length in sorted(summary.aerial_lengths.items()):
            lines.append(f"- {wire_type}: {length:,.2f} ft")
        lines.append("")
    
    # Other
    if summary.other_lengths:
        lines.append("Other Categories:")
        lines.append("-" * 20)
        for category, length in sorted(summary.other_lengths.items()):
            lines.append(f"- {category}: {length:,.2f} ft")
        lines.append("")
    
    # Project totals
    lines.append("Project Totals:")
    lines.append("-" * 20)
    lines.append(f"- Total segments: {summary.segment_count}")
    lines.append(f"- Total length: {summary.total_length:,.2f} ft")
    
    return "\n".join(lines)
