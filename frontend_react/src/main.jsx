import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import Landing from './Landing.jsx'

function Root() {
  const [view, setView] = useState('landing') // 'landing' | 'app'
  const [tab, setTab] = useState('gis')

  if (view === 'landing') {
    return <Landing onEnter={(t) => { setTab(t); setView('app') }} />
  }
  return <App key={tab} initialTab={tab} onHome={() => setView('landing')} />
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
