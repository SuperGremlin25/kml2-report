"""Test the geometry analyzer module."""

import pytest
from unittest.mock import Mock, patch
from kml2report.models import Feature, Geometry
from kml2report.geometry_analyzer import (
    parse_description_html,
    extract_duct_info,
    aggregate_project_footage,
    calculate_linestring_length,
    generate_summary_report
)


def test_parse_description_html():
    """Test HTML description parsing."""
    # Test with table data
    html = """
    <table>
        <tr>
            <td>Duct_Size</td>
            <td>2"</td>
        </tr>
        <tr>
            <td>New_Length</td>
            <td>106.507453</td>
        </tr>
    </table>
    """
    
    result = parse_description_html(html)
    assert result == {
        'Duct_Size': '2"',
        'Duct_Package': 'single',
        'Duct_Count': '1',
        'New_Length': '106.507453'
    }
    
    # Test with empty description
    assert parse_description_html("") == {}
    assert parse_description_html(None) == {}


def test_extract_duct_info():
    """Test duct information extraction."""
    # Create a mock feature
    feature = Mock(spec=Feature)
    feature.name = '2"'
    feature.raw_description = """
    <table>
        <tr>
            <td>Duct_Size</td>
            <td>2"</td>
        </tr>
        <tr>
            <td>New_Length</td>
            <td>106.507453</td>
        </tr>
    </table>
    """
    feature.placemark_id = 'test_001'
    
    duct_info = extract_duct_info(feature)
    
    assert duct_info.duct_size == '2"'
    assert duct_info.length == 106.507453
    assert duct_info.feature_id == 'test_001'


def test_calculate_linestring_length():
    """Test length calculation for LineString."""
    # Create a simple LineString (2 points)
    geometry = Geometry(
        geometry_type='LineString',
        coordinates=[
            (-99.0, 34.0, 0.0),
            (-99.001, 34.0, 0.0)
        ]
    )
    
    # Should return a positive length
    length = calculate_linestring_length(geometry)
    assert length > 0
    
    # Test with non-LineString
    point_geom = Geometry(geometry_type='Point', coordinates=[(-99.0, 34.0, 0.0)])
    assert calculate_linestring_length(point_geom) == 0
    
    # Test with empty coordinates
    empty_geom = Geometry(geometry_type='LineString', coordinates=[])
    assert calculate_linestring_length(empty_geom) == 0


def test_aggregate_project_footage():
    """Test project footage aggregation."""
    # Create mock features
    features = []
    
    # Add 2" duct features
    for i in range(3):
        feature = Mock(spec=Feature)
        feature.name = '2"'
        feature.raw_description = f"""
        <table>
            <tr>
                <td>New_Length</td>
                <td>{100 + i * 10}</td>
            </tr>
        </table>
        """
        feature.placemark_id = f'2inch_{i}'
        
        geometry = Mock(spec=Geometry)
        geometry.geometry_type = 'LineString'
        feature.geometry = geometry
        
        features.append(feature)
    
    # Add 3" duct features
    for i in range(2):
        feature = Mock(spec=Feature)
        feature.name = '3"'
        feature.raw_description = f"""
        <table>
            <tr>
                <td>New_Length</td>
                <td>{200 + i * 20}</td>
            </tr>
        </table>
        """
        feature.placemark_id = f'3inch_{i}'
        
        geometry = Mock(spec=Geometry)
        geometry.geometry_type = 'LineString'
        feature.geometry = geometry
        
        features.append(feature)
    
    # Aggregate
    summary = aggregate_project_footage(features)
    
    # Verify results
    assert summary.segment_count == 5
    assert summary.total_length == 100 + 110 + 120 + 200 + 220
    assert summary.single_duct_lengths['2"'] == 100 + 110 + 120
    assert summary.single_duct_lengths['3"'] == 200 + 220


def test_generate_summary_report():
    """Test summary report generation."""
    from kml2report.geometry_analyzer import ProjectSummary
    
    # Create a mock summary
    summary = ProjectSummary()
    summary.segment_count = 5
    summary.total_length = 750.0
    summary.single_duct_lengths = {
        '2"': 330.0,
        '3"': 220.0,
        '4"': 200.0
    }
    
    # Generate report
    report = generate_summary_report(summary)
    
    # Verify report contains expected content
    assert "Project Summary - Total Footage:" in report
    assert "Single Duct Runs:" in report
    assert '2" duct: 330.00 ft' in report
    assert '3" duct: 220.00 ft' in report
    assert '4" duct: 200.00 ft' in report
    assert "Total linear segments: 5" in report
    assert "Total linear length: 750.00 ft" in report


if __name__ == "__main__":
    # Run basic tests
    test_parse_description_html()
    test_extract_duct_info()
    test_calculate_linestring_length()
    test_aggregate_project_footage()
    test_generate_summary_report()
    print("All tests passed!")
