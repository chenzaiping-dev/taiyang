from marshmallow import Schema, fields, validate, validates_schema, ValidationError


class ProcessOrderMaterialSchema(Schema):
    material_id = fields.Int(required=True, validate=validate.Range(min=1))
    ratio_value = fields.Decimal(
        required=True, places=8, as_string=True,
        validate=validate.Range(min=0, max=1),
    )
    quantity = fields.Decimal(
        required=False, places=6, as_string=True,
        load_default="0", validate=validate.Range(min=0),
    )
    cost = fields.Decimal(
        required=False, places=6, as_string=True,
        load_default="0", validate=validate.Range(min=0),
    )
    remark = fields.Str(required=False, allow_none=True, validate=validate.Length(max=255))


class ProcessOrderRequestSchema(Schema):
    process_order_no = fields.Str(required=False, allow_none=True, validate=validate.Length(max=50))
    factory_id = fields.Int(required=True)
    performance_id = fields.Int(required=True)
    followup_employee_id = fields.Int(required=True)
    created_by = fields.Int(required=True)

    batch_no = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    product_name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    product_specification = fields.Str(required=False, allow_none=True, validate=validate.Length(max=500))
    unit = fields.Str(required=False, load_default="KG")
    quantity = fields.Decimal(required=True, places=6, as_string=True,
                              validate=validate.Range(min=0.000001))

    order_time = fields.DateTime(required=False, allow_none=True)
    cost_price = fields.Decimal(required=False, as_string=True, load_default="0")
    total_cost = fields.Decimal(required=False, as_string=True, load_default="0")
    remark = fields.Str(required=False, allow_none=True, validate=validate.Length(max=500))
    status = fields.Str(required=False, load_default="DRAFT",
                        validate=validate.OneOf(["DRAFT", "ORDERED"]))

    materials = fields.List(fields.Nested(ProcessOrderMaterialSchema),
                            required=True, validate=validate.Length(min=1))

    @validates_schema
    def check_ratio_sum(self, data, **kwargs):
        items = data.get("materials") or []
        ids = [m["material_id"] for m in items]
        if len(ids) != len(set(ids)):
            raise ValidationError("同一原料只能出现一次", "materials")
        # 配比合计 = 1（允许 1e-6 误差）
        from decimal import Decimal
        total = sum((Decimal(str(m["ratio_value"])) for m in items), Decimal("0"))
        if abs(total - Decimal("1")) > Decimal("0.000001"):
            raise ValidationError(
                f"原料配比合计必须为 1，当前为 {total}", "materials"
            )