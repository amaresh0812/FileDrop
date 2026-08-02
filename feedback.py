

/* 1. Base Button Styling */
.btn-secondary { 
  background: var(--surface); 
  color: var(--text); 
  border: 1px solid var(--border); 
  position: relative;
  
  outline: none;
  cursor: pointer;
  padding: 20px 100px;
  border-radius: 11px;
  font-size: 24px;
  overflow: hidden;
  letter-spacing: 8px;
  z-index: 1;
  /* Smooth transition for text and borders */
  transition: color 0.4s ease, border-color 0.4s ease, background-color 0.4s ease;
}

/* 2. Hover Interactions for Text and Border Colors */
.btn-secondary:hover { 
  color: #04110c; 
  border-color: #2b7fff;
}

/* 3. Wave Container Layer */
.btn-secondary .ocean {
  position: absolute;
  left: 0;
  bottom: -150px; /* Hide the waves completely below the button view initially */
  width: 100%;
  height: 250px;
  transition: bottom 0.6s cubic-bezier(0.19, 1, 0.22, 1); /* Premium fluid rise effect */
  pointer-events: none;
  z-index: -1;
}

/* 4. Trigger Wave Rise on Hover */
.btn-secondary:hover .ocean {
  bottom: -60px; /* Pulls the spinning gradient waves upward to fill the button */
}

/* 5. Wave Elements (Styled with your explicit linear gradient colors) */
.ocean:before, .ocean:after {
  content: '';
  position: absolute;
  width: 300%; 
  height: 300%;
  top: 0;
  left: 50%;
  background: linear-gradient(135deg, var(--accent), #2b7fff);
}

/* 6. Front Wave Properties */
.ocean:before {
  border-radius: 43%;
  opacity: 1;
  animation: moveOcean 7s linear infinite;
}

/* 7. Back Wave Properties (Slightly offset for depth) */
.ocean:after {
  border-radius: 40%;
  opacity: 0.6; /* Creates depth using transparency against the gradient */
  animation: moveOcean 12s linear infinite;
}

/* 8. Text Layer Safeguard */
.description {
  z-index: 2;
  position: relative;
}

/* 9. Rotation Animation */
@keyframes moveOcean {
  0% {
    transform: translate(-50%, -75%) rotate(0deg);
  }
  100% {
    transform: translate(-50%, -75%) rotate(360deg);
  }
}
