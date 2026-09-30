// 共享的简单内联样式常量。项目约定用内联样式（无 CSS 文件 / CSS Modules），
// 卡片与表格在多个页面重复出现，抽到一处避免各处复制粘贴后口径漂移。

import type { CSSProperties } from 'react';

export const cardStyle: CSSProperties = {
  border: '1px solid #e0e0e0',
  borderRadius: 8,
  padding: 16,
  marginBottom: 16,
  background: '#fff',
};

export const tableStyle: CSSProperties = {
  borderCollapse: 'collapse',
  width: '100%',
  fontSize: 14,
};

export const thStyle: CSSProperties = {
  textAlign: 'left',
  padding: '6px 10px',
  borderBottom: '2px solid #e0e0e0',
  background: '#f8f8f8',
  whiteSpace: 'nowrap',
};

export const tdStyle: CSSProperties = {
  padding: '6px 10px',
  borderBottom: '1px solid #eee',
  verticalAlign: 'top',
};

export const buttonStyle: CSSProperties = {
  padding: '6px 12px',
  border: '1px solid #ccc',
  borderRadius: 6,
  background: '#fff',
  cursor: 'pointer',
};

export const inputStyle: CSSProperties = {
  padding: '6px 8px',
  border: '1px solid #ccc',
  borderRadius: 6,
};
