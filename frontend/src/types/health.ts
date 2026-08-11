/* 健康检查类型 —— 对应后端 GET /health（main.py, response_model=HealthResponse）。
   - status: 后端存活标记（顶栏红绿灯）。
   - capabilities: 全局能力开关，前端据此驱动菜单/路由/页面可见性（准则 F2 能力驱动 UI）。
     单一真相源在后端 settings，前端零配置、永不漂移。 */
export interface Capabilities {
  kb_enabled: boolean
}

export interface HealthStatus {
  status: string
  capabilities: Capabilities
}
