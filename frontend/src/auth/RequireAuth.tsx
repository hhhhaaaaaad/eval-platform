// 路由守卫：未登录跳转登录页，并记录来源路径，登录成功后回跳。
// 登录态来源是 localStorage（session.ts），刷新后仍有效。

import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { isAuthenticated } from '../api/session';

export default function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation();

  if (!isAuthenticated()) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  return <>{children}</>;
}
