"""锁死 connector-gw 的对外路由：只有两个健康检查。

写只能经 api 进程内的 ``WriteGateway``；网关一旦长出业务/写端点，就等于开了第二套
入口。这条用例变红时不要去改断言，先问「为什么网关需要业务路由」（B-GW-NOCALLER）。
"""

from apps.connectors.gateway import create_app


def test_connector_gateway_exposes_only_health_routes() -> None:
    app = create_app()
    routes = {route.path: set(getattr(route, "methods", set())) for route in app.routes}

    assert set(routes) == {"/healthz", "/readyz"}
    assert all(methods <= {"GET", "HEAD"} for methods in routes.values())
