import { NavLink, Outlet } from "react-router-dom";

import zstackLogo from "../assets/zstack-logo.svg";

const navigation = [
  { to: "/", label: "公司总览", end: true, marker: "01" },
  { to: "/reviews", label: "候选审核", marker: "02" },
  { to: "/decisions", label: "正式决策", marker: "03" },
  { to: "/publications", label: "发布历史", marker: "04" },
];

export function AppShell() {
  return (
    <div className="app-frame">
      <aside className="rail">
        <div className="brand">
          <img src={zstackLogo} alt="ZStack" />
          <span className="brand__divider" aria-hidden="true" />
          <span className="brand__product">ZDecision</span>
        </div>
        <nav className="rail__nav" aria-label="主导航">
          {navigation.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `rail__link${isActive ? " rail__link--active" : ""}`
              }
            >
              <span className="rail__marker" aria-hidden="true">
                {item.marker}
              </span>
              <span>{item.label}</span>
            </NavLink>
          ))}
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
