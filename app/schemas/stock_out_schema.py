from marshmallow import Schema, fields, validate, validates_schema, ValidationError


class StockOutLineSchema(Schema):
    line_no = fields.Int(required=False, allow_none=True)
    material_id = fields.Int(required=True, validate=validate.Range(min=1))
    quantity = fields.Decimal(
        required=True, places=6, as_string=True,
        validate=validate.Range(min=0.000001),
    )


class StockOutRequestSchema(Schema):
    document_no = fields.Str(required=False, allow_none=True, validate=validate.Length(max=50))
    warehouse_id = fields.Int(required=True)
    out_type = fields.Str(
        required=False, load_default="PRODUCTION_OUT",
        validate=validate.OneOf(["PRODUCTION_OUT", "STOCKTAKE_LOSS"]),
    )
    process_order_id = fields.Int(required=False, allow_none=True)
    document_time = fields.DateTime(required=False, allow_none=True)
    remark = fields.Str(required=False, allow_none=True, validate=validate.Length(max=500))
    auto_confirm = fields.Bool(required=False, load_default=True)
    allow_partial = fields.Bool(required=False, load_default=False)
    lines = fields.List(
        fields.Nested(StockOutLineSchema),
        required=True,
        validate=validate.Length(min=1),
    )

    @validates_schema
    def check_dup(self, data, **kwargs):
        ids = [l["material_id"] for l in data.get("lines", [])]
        if len(ids) != len(set(ids)):
            raise ValidationError("同一原料只能出现一次")