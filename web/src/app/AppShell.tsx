import { NavLink, Outlet, useLocation } from "react-router-dom";

import zstackLogo from "../assets/zstack-logo.svg";

const navigation = [
  { to: "/", label: "公司总览", end: true, marker: "01" },
  {
    to: "/reviews",
    label: "候选审核",
    marker: "02",
    owns: (path: string) =>
      path.startsWith("/publication-previews/") ||
      (path.startsWith("/spaces/") && path.endsWith("/candidates")),
  },
  {
    to: "/decisions",
    label: "正式决策",
    marker: "03",
    owns: (path: string) =>
      path.startsWith("/spaces/") && path.includes("/decisions"),
  },
  {
    to: "/publications",
    label: "发布历史",
    marker: "04",
    owns: (path: string) =>
      path.startsWith("/spaces/") && path.endsWith("/publications"),
  },
];

export function AppShell() {
  const { pathname } = useLocation();

  return (
    <div className="app-frame">
      <aside className="rail">
        <div className="brand">
          <img src={zstackLogo} alt="ZStack" />
          <span className="brand__divider" aria-hidden="true" />
          <span className="brand__product">ZDecision</span>
        </div>
        <nav className="rail__nav" aria-label="主导航">
          {navigation.map((item) => {
            const ownsPath = item.owns?.(pathname) ?? false;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                aria-current={ownsPath ? "page" : undefined}
                className={({ isActive }) =>
                  `rail__link${isActive || ownsPath ? " rail__link--active" : ""}`
                }
              >
                <span className="rail__marker" aria-hidden="true">
                  {item.marker}
                </span>
                <span>{item.label}</span>
              </NavLink>
            );
          })}
        </nav>
        <div className="rail__foot">
          <span className="rail__pulse" aria-hidden="true" />
          中央决策服务
        </div>
      </aside>
      <main className="main-canvas">
        <Outlet />
      </main>
    </div>
  );
}
