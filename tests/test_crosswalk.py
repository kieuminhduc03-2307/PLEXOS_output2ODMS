from __future__ import annotations

from pathlib import Path

from plexos_output2odms.crosswalk.generator_dispatch import build_rts_gmlc_crosswalk


def test_crosswalk_matches_exact_bus_capacity_and_target_mrid(tmp_path: Path):
    model = tmp_path / "model.xml"
    model.write_text(
        """<MasterDataSet>
<t_class><class_id>1</class_id><name>System</name></t_class>
<t_class><class_id>2</class_id><name>Generator</name></t_class>
<t_class><class_id>20</class_id><name>Node</name></t_class>
<t_object><object_id>1</object_id><class_id>1</class_id><name>System</name></t_object>
<t_object><object_id>8</object_id><class_id>2</class_id><name>101_CT_1</name><GUID>guid-1</GUID></t_object>
<t_object><object_id>9</object_id><class_id>2</class_id><name>101_STEAM_2</name><GUID>guid-2</GUID></t_object>
<t_object><object_id>100</object_id><class_id>20</class_id><name>101</name></t_object>
<t_collection><collection_id>1</collection_id><parent_class_id>1</parent_class_id><child_class_id>2</child_class_id><name>Generators</name></t_collection>
<t_collection><collection_id>5</collection_id><parent_class_id>2</parent_class_id><child_class_id>20</child_class_id><name>Nodes</name></t_collection>
<t_membership><membership_id>1</membership_id><collection_id>1</collection_id><parent_object_id>1</parent_object_id><child_object_id>8</child_object_id></t_membership>
<t_membership><membership_id>2</membership_id><collection_id>1</collection_id><parent_object_id>1</parent_object_id><child_object_id>9</child_object_id></t_membership>
<t_membership><membership_id>3</membership_id><collection_id>5</collection_id><parent_object_id>8</parent_object_id><child_object_id>100</child_object_id></t_membership>
<t_membership><membership_id>4</membership_id><collection_id>5</collection_id><parent_object_id>9</parent_object_id><child_object_id>100</child_object_id></t_membership>
<t_property><collection_id>1</collection_id><property_id>1</property_id><name>Max Capacity</name></t_property>
<t_data><membership_id>1</membership_id><property_id>1</property_id><value>20</value></t_data>
<t_data><membership_id>2</membership_id><property_id>1</property_id><value>76</value></t_data>
</MasterDataSet>""",
        encoding="utf-8",
    )
    cim = tmp_path / "target.xml"
    cim.write_text(
        """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:cim="http://iec.ch/TC57/CIM100#">
<cim:GeneratingUnit rdf:ID="gu1"><cim:GeneratingUnit.minOperatingP>8</cim:GeneratingUnit.minOperatingP><cim:GeneratingUnit.maxOperatingP>20</cim:GeneratingUnit.maxOperatingP></cim:GeneratingUnit>
<cim:GeneratingUnit rdf:ID="gu2"><cim:GeneratingUnit.minOperatingP>30</cim:GeneratingUnit.minOperatingP><cim:GeneratingUnit.maxOperatingP>76</cim:GeneratingUnit.maxOperatingP></cim:GeneratingUnit>
<cim:SynchronousMachine rdf:ID="sm1"><cim:IdentifiedObject.name>101_1</cim:IdentifiedObject.name><cim:RotatingMachine.GeneratingUnit rdf:resource="#gu1"/></cim:SynchronousMachine>
<cim:SynchronousMachine rdf:ID="sm2"><cim:IdentifiedObject.name>101_2</cim:IdentifiedObject.name><cim:RotatingMachine.GeneratingUnit rdf:resource="#gu2"/></cim:SynchronousMachine>
</rdf:RDF>""",
        encoding="utf-8",
    )
    mappings = build_rts_gmlc_crosswalk(model, cim, approved=True)
    assert [(item.source_name, item.odms_machine_name) for item in mappings] == [
        ("101_CT_1", "101_1"),
        ("101_STEAM_2", "101_2"),
    ]
    assert all(item.approved for item in mappings)
