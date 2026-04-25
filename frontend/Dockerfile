# frontend/Dockerfile
FROM node:18-alpine

WORKDIR /app

# Install dependencies
COPY package*.json ./
RUN npm install

# Build for production
COPY . .
RUN npm run build

EXPOSE 3000

# Start development server with host binding
CMD ["npm", "run", "dev"]