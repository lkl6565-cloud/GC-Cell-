FROM eclipse-temurin:17-jdk-alpine
VOLUME /tmp
COPY src /app/src
COPY pom.xml /app/pom.xml
WORKDIR /app
RUN apk add --no-cache maven && mvn clean package -DskipTests
RUN cp target/*.jar /app.jar
ENTRYPOINT ["java","-jar","/app.jar"]
