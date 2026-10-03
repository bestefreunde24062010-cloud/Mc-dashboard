plugins {
    java
}

group = property("pluginGroup") as String
version = property("pluginVersion") as String

repositories {
    mavenCentral()

    maven {
        name = "papermc"
        url = uri("https://repo.papermc.io/repository/maven-public/")
    }
}

dependencies {
    compileOnly("com.velocitypowered:velocity-api:${property("velocityVersion")}")
    annotationProcessor("com.velocitypowered:velocity-api:${property("velocityVersion")}")

    implementation("com.moandjiezana.toml:toml4j:0.7.2")

    compileOnly("com.google.code.gson:gson:2.10.1")
}

tasks.withType<JavaCompile> {
    options.encoding = "UTF-8"
    options.release.set(17)
}

tasks.jar {
    archiveBaseName.set("mc-dashboard")
    archiveVersion.set(version.toString())

    from({
        configurations.runtimeClasspath.get()
            .filter { it.name.contains("toml4j") }
            .map { if (it.isDirectory) it else zipTree(it) }
    })

    duplicatesStrategy = DuplicatesStrategy.EXCLUDE

    manifest {
        attributes(
            "Implementation-Title" to "MC Dashboard",
            "Implementation-Version" to version
        )
    }
}
