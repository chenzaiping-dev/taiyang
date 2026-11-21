from sqlalchemy import or_, false, text

from app.extensions import db
from app.services.permission_service import (
    get_data_scopes,
    get_current_employee_id,
)


def apply_sales_performance_scope(
    query,
    model,
    permission_code="sales_performance.view",
):
    """
    对业绩单查询应用数据范围。

    own:
        created_by = 当前员工
        OR co_salesperson_id = 当前员工

    assigned:
        main_salesperson_id = 当前员工
        OR co_salesperson_id = 当前员工

    all:
        不增加过滤条件
    """
    scopes = get_data_scopes(permission_code)

    if "all" in scopes:
        return query

    employee_id = get_current_employee_id()

    if not employee_id:
        return query.filter(false())

    conditions = []

    if "own" in scopes:
        conditions.append(
            or_(
                model.created_by == employee_id,
                model.co_salesperson_id == employee_id,
            )
        )

    if "assigned" in scopes:
        conditions.append(
            or_(
                model.main_salesperson_id == employee_id,
                model.co_salesperson_id == employee_id,
            )
        )

    if not conditions:
        return query.filter(false())

    return query.filter(or_(*conditions))


def can_access_sales_performance(
    performance_id,
    permission_code="sales_performance.view",
):
    """
    检查当前员工是否有权访问指定业绩单。
    用于详情、编辑、删除、状态流转等单条记录操作。
    """
    scopes = get_data_scopes(permission_code)

    if "all" in scopes:
        return True

    employee_id = get_current_employee_id()

    if not employee_id:
        return False

    conditions = []

    if "own" in scopes:
        conditions.append(
            "(created_by = :employee_id "
            "OR co_salesperson_id = :employee_id)"
        )

    if "assigned" in scopes:
        conditions.append(
            "(main_salesperson_id = :employee_id "
            "OR co_salesperson_id = :employee_id)"
        )

    if not conditions:
        return False

    sql = f"""
        SELECT 1
          FROM sales_performance
         WHERE id = :performance_id
           AND ({' OR '.join(conditions)})
         LIMIT 1
    """

    result = db.session.execute(
        text(sql),
        {
            "performance_id": performance_id,
            "employee_id": employee_id,
        },
    ).first()

    return result is not None