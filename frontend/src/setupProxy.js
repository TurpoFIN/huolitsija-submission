// Local development only: proxy /api to the backend (Emergent's ingress does this in preview and production).
const { createProxyMiddleware } = require("http-proxy-middleware");

module.exports = function (app) {
  const target = process.env.API_PROXY_TARGET || "http://localhost:8001";
  app.use(createProxyMiddleware("/api", { target, changeOrigin: false, ws: false }));
};
