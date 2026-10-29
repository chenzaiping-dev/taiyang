from datetime import datetime
from marshmallow import Schema, fields, validate, ValidationError


class PurchaseInLineSchema(Schema):
    line_no = fields.Int(required=False, allow_none=True)
    material_id = fields.Int(required=True, validate=validate.Range(min=1))
    quantity = fields.Decimal(
        required=True, places=6, as_string=True, validate=validate.Range(min=0.000001)
    )
    unit_cost = fields.Decimal(
        required=True, places=6, as_string=True, validate=validate.Range(min=0)
    )


class PurchaseInRequestSchema(Schema):
    document_no = fields.Str(required=False, allow_none=True, validate=validate.Length(max=50))
    warehouse_id = fields.Int(required=True)
    supplier_id = fields.Int(required=True)
    document_time = fields.DateTime(required=False, allow_none=True)
    remark = fields.Str(required=False, allow_none=True, validate=validate.Length(max=500))
    auto_confirm = fields.Bool(required=False, load_default=True)
    lines = fields.List(
        fields.Nested(PurchaseInLineSchema),
        required=True,
        validate=validate.Length(min=1),
    )