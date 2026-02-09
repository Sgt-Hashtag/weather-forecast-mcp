# frontend/Dockerfile
FROM node:18-alpine

WORKDIR /app

# Install dependencies first (better caching)
COPY package*.json ./
RUN npm install

# Copy source code
COPY . .

EXPOSE 3000

# Start development server with host binding
CMD ["npm", "run", "dev"]