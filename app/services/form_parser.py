from decimal import Decimal, InvalidOperation


class FormParseError(Exception):
    pass


def parse_purchase_in_form(form) -> dict:
    """
    将 Werkzeug MultiDict 转成 Service 需要的 dict。

    支持的行字段命名：
        lines[0][material_id]
        lines[0][quantity]
        lines[0][unit_cost]
    """
    warehouse_id = form.get("warehouse_id", type=int)
    supplier_id = form.get("supplier_id", type=int)
    if not warehouse_id:
        raise FormParseError("请选择仓库")
    if not supplier_id:
        raise FormParseError("请选择供应商")

    # ---- 收集动态行 ----
    # 通过 material_id 的 key 形式判断行数
    line_indexes = set()
    for key in form.keys():
        if key.startswith("lines[") and "][material_id]" in key:
            idx = key.split("[")[1].split("]")[0]
            if idx.isdigit():
                line_indexes.add(int(idx))

    lines = []
    for idx in sorted(line_indexes):
        mid_raw = form.get(f"lines[{idx}][material_id]", "").strip()
        qty_raw = form.get(f"lines[{idx}][quantity]", "").strip()
        cost_raw = form.get(f"lines[{idx}][unit_cost]", "").strip()

        # 空行直接跳过（用户加行但没填）
        if not mid_raw and not qty_raw and not cost_raw:
            continue

        if not mid_raw:
            raise FormParseError(f"第 {idx + 1} 行：请选择原料")
        try:
            mid = int(mid_raw)
        except ValueError:
            raise FormParseError(f"第 {idx + 1} 行：原料ID非法")

        if not qty_raw:
            raise FormParseError(f"第 {idx + 1} 行：请填写数量")
        if not cost_raw:
            raise FormParseError(f"第 {idx + 1} 行：请填写单价")

        try:
            qty = Decimal(qty_raw)
            cost = Decimal(cost_raw)
        except InvalidOperation:
            raise FormParseError(f"第 {idx + 1} 行：数量或单价格式错误")

        if qty <= 0:
            raise FormParseError(f"第 {idx + 1} 行：数量必须大于 0")
        if cost < 0:
            raise FormParseError(f"第 {idx + 1} 行：单价不能为负数")

        lines.append(
            {
                "line_no": idx + 1,
                "material_id": mid,
                "quantity": str(qty),
                "unit_cost": str(cost),
            }
        )

    if not lines:
        raise FormParseError("请至少填写一行有效原料明细")

    return {
        "warehouse_id": warehouse_id,
        "supplier_id": supplier_id,
        "document_time": form.get("document_time") or None,
        "remark": form.get("remark") or None,
        "auto_confirm": form.get("auto_confirm") == "on",
        "lines": lines,
    }

def parse_stock_out_form(form) -> dict:
    """
    将 HTML form 转成 Service 需要的 dict。
    支持：lines[0][material_id]、lines[0][quantity]
    """
    warehouse_id = form.get("warehouse_id", type=int)
    if not warehouse_id:
        raise FormParseError("请选择仓库")

    out_type = form.get("out_type", "PRODUCTION_OUT")
    if out_type not in ("PRODUCTION_OUT", "STOCKTAKE_LOSS"):
        raise FormParseError("出库类型非法")

    # 收集行
    line_indexes = set()
    for key in form.keys():
        if key.startswith("lines[") and "][material_id]" in key:
            idx = key.split("[")[1].split("]")[0]
            if idx.isdigit():
                line_indexes.add(int(idx))

    lines = []
    for idx in sorted(line_indexes):
        mid_raw = form.get(f"lines[{idx}][material_id]", "").strip()
        qty_raw = form.get(f"lines[{idx}][quantity]", "").strip()
        if not mid_raw and not qty_raw:
            continue
        if not mid_raw:
            raise FormParseError(f"第 {idx + 1} 行：请选择原料")
        if not qty_raw:
            raise FormParseError(f"第 {idx + 1} 行：请填写数量")
        try:
            mid = int(mid_raw)
            qty = Decimal(qty_raw)
        except (ValueError, InvalidOperation):
            raise FormParseError(f"第 {idx + 1} 行：格式错误")
        if qty <= 0:
            raise FormParseError(f"第 {idx + 1} 行：数量必须大于 0")
        lines.append({
            "line_no": idx + 1,
            "material_id": mid,
            "quantity": str(qty),
        })

    if not lines:
        raise FormParseError("请至少填写一行有效明细")

    process_order_id = form.get("process_order_id", type=int)
    return {
        "warehouse_id": warehouse_id,
        "out_type": out_type,
        "process_order_id": process_order_id or None,
        "document_time": form.get("document_time") or None,
        "remark": form.get("remark") or None,
        "auto_confirm": form.get("auto_confirm") == "on",
        "allow_partial": form.get("allow_partial") == "on",
        "lines": lines,
    }

def parse_process_order_form(form) -> dict:
    """
    解析新建工艺单 HTML form。
    行字段：materials[0][material_id]、materials[0][ratio_value]、
           materials[0][quantity]、materials[0][cost]、materials[0][remark]
    """
    factory_id = form.get("factory_id", type=int)
    performance_id = form.get("performance_id", type=int)
    followup_employee_id = form.get("followup_employee_id", type=int)
    created_by = form.get("created_by", type=int)

    if not factory_id:
        raise FormParseError("请选择加工厂")
    if not performance_id:
        raise FormParseError("请选择业绩单")
    if not followup_employee_id:
        raise FormParseError("请选择跟单员")
    if not created_by:
        raise FormParseError("缺少创建人")

    batch_no = form.get("batch_no", "").strip()
    product_name = form.get("product_name", "").strip()
    if not batch_no:
        raise FormParseError("请填写生产批号")
    if not product_name:
        raise FormParseError("请填写产品名称")

    qty_raw = form.get("quantity", "").strip()
    if not qty_raw:
        raise FormParseError("请填写计划生产数量")
    try:
        quantity = Decimal(qty_raw)
    except InvalidOperation:
        raise FormParseError("计划生产数量格式错误")
    if quantity <= 0:
        raise FormParseError("计划生产数量必须大于 0")

    # ---- 明细 ----
    line_indexes = set()
    for key in form.keys():
        if key.startswith("materials[") and "][material_id]" in key:
            idx = key.split("[")[1].split("]")[0]
            if idx.isdigit():
                line_indexes.add(int(idx))

    materials = []
    for idx in sorted(line_indexes):
        mid_raw = form.get(f"materials[{idx}][material_id]", "").strip()
        ratio_raw = form.get(f"materials[{idx}][ratio_value]", "").strip()
        qty_line_raw = form.get(f"materials[{idx}][quantity]", "").strip()
        cost_raw = form.get(f"materials[{idx}][cost]", "").strip()
        remark = form.get(f"materials[{idx}][remark]", "").strip() or None

        if not mid_raw and not ratio_raw:
            continue   # 空行跳过
        if not mid_raw:
            raise FormParseError(f"第 {idx + 1} 行：请选择原料")
        if not ratio_raw:
            raise FormParseError(f"第 {idx + 1} 行：请填写配比")

        try:
            mid = int(mid_raw)
            ratio = Decimal(ratio_raw)
        except (ValueError, InvalidOperation):
            raise FormParseError(f"第 {idx + 1} 行：格式错误")

        if ratio < 0 or ratio > 1:
            raise FormParseError(f"第 {idx + 1} 行：配比必须在 0~1 之间")

        qty_line = Decimal("0")
        if qty_line_raw:
            try:
                qty_line = Decimal(qty_line_raw)
            except InvalidOperation:
                raise FormParseError(f"第 {idx + 1} 行：数量格式错误")
            if qty_line < 0:
                raise FormParseError(f"第 {idx + 1} 行：数量不能为负数")

        cost = Decimal("0")
        if cost_raw:
            try:
                cost = Decimal(cost_raw)
            except InvalidOperation:
                raise FormParseError(f"第 {idx + 1} 行：成本格式错误")
            if cost < 0:
                raise FormParseError(f"第 {idx + 1} 行：成本不能为负数")

        materials.append({
            "material_id": mid,
            "ratio_value": str(ratio),
            "quantity": str(qty_line),
            "cost": str(cost),
            "remark": remark,
        })

    if not materials:
        raise FormParseError("请至少填写一条原料配比")

    return {
        "factory_id": factory_id,
        "performance_id": performance_id,
        "followup_employee_id": followup_employee_id,
        "created_by": created_by,
        "batch_no": batch_no,
        "product_name": product_name,
        "product_specification": form.get("product_specification", "").strip() or None,
        "unit": form.get("unit", "KG").strip() or "KG",
        "quantity": str(quantity),
        "order_time": form.get("order_time") or None,
        "cost_price": form.get("cost_price", "0").strip() or "0",
        "total_cost": form.get("total_cost", "0").strip() or "0",
        "remark": form.get("remark", "").strip() or None,
        "process_order_no": form.get("process_order_no", "").strip() or None,
        "status": form.get("status", "DRAFT"),
        "materials": materials,
    }