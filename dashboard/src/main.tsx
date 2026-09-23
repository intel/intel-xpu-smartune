import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import './styles/global.css'
import App from './App'
import { darkTheme, lightTheme, setColorMode } from './styles/theme'
import type { ColorMode } from './styles/theme'

const THEME_STORAGE_KEY = 'smartune-color-mode'

function readColorMode(): ColorMode {
  return localStorage.getItem(THEME_STORAGE_KEY) === 'light' ? 'light' : 'dark'
}

function DashboardRoot() {
  const [colorMode, setColorModeState] = React.useState<ColorMode>(readColorMode)
  setColorMode(colorMode)

  React.useEffect(() => {
    document.documentElement.dataset.theme = colorMode
    localStorage.setItem(THEME_STORAGE_KEY, colorMode)
  }, [colorMode])

  return (
    <ConfigProvider theme={colorMode === 'dark' ? darkTheme : lightTheme}>
      <App colorMode={colorMode} onColorModeChange={setColorModeState} />
    </ConfigProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <DashboardRoot />
  </React.StrictMode>,
)
